"""Надёжная топология RabbitMQ и ограниченные повторы без горячего цикла."""
import asyncio
import json
import logging
import aio_pika
from event_bus.contracts import EVENT_TYPES, InvalidEvent, decode_event
from event_bus.observability import configure_json_logging, correlation_scope, event_correlation_id, event_log_fields

log = logging.getLogger(__name__)
BINDINGS = {
    "kitchen": ("order.paid", "order.cancelled"),
    "delivery": ("order.paid", "order.cancelled"),
    "compensation": ("delivery.failed",),
}


class EventBus:
    def __init__(self, url, prefix="", retry_delay_ms=1000):
        self.url = url
        self.prefix = prefix
        self.retry_delay_ms = retry_delay_ms
        self.connection = None
        self.channel = None
        self.queues = {}

    def name(self, value):
        return self.prefix + value

    async def __aenter__(self):
        self.connection = await aio_pika.connect_robust(self.url, timeout=5)
        try:
            self.channel = await self.connection.channel(
                publisher_confirms=True, on_return_raises=True,
            )
            await self.channel.set_qos(prefetch_count=10)
            self.exchange = await self.channel.declare_exchange(
                self.name("sirius_events"), aio_pika.ExchangeType.TOPIC, durable=True,
            )
            self.dlx = await self.channel.declare_exchange(
                self.name("dlx_exchange"), aio_pika.ExchangeType.DIRECT, durable=True,
            )
            self.retry = await self.channel.declare_exchange(
                self.name("retry_exchange"), aio_pika.ExchangeType.DIRECT, durable=True,
            )
            # Все очереди объявляются ДО первой публикации, даже без consumer'ов.
            for consumer, keys in BINDINGS.items():
                dlq = await self.channel.declare_queue(self.name(f"{consumer}_dlq"), durable=True)
                await dlq.bind(self.dlx, routing_key=f"{consumer}.dlq")
                queue = await self.channel.declare_queue(
                    self.name(f"{consumer}_queue"), durable=True,
                    arguments={
                        "x-dead-letter-exchange": self.dlx.name,
                        "x-dead-letter-routing-key": f"{consumer}.dlq",
                    },
                )
                for key in keys:
                    await queue.bind(self.exchange, routing_key=key)
                self.queues[consumer] = queue
                retry_queue = await self.channel.declare_queue(
                    self.name(f"{consumer}_retry"), durable=True,
                    arguments={
                        "x-message-ttl": self.retry_delay_ms,
                        "x-dead-letter-exchange": "",
                        "x-dead-letter-routing-key": queue.name,
                    },
                )
                await retry_queue.bind(self.retry, routing_key=consumer)
            return self
        except BaseException:
            await self.connection.close()
            raise

    async def __aexit__(self, *args):
        await self.connection.close()

    async def publish_event(self, routing_key, payload):
        await self.exchange.publish(
            aio_pika.Message(
                json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(),
                content_type="application/json", message_id=payload["event_id"],
                correlation_id=event_correlation_id(payload),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ), routing_key=routing_key, mandatory=True, timeout=5,
        )

    async def dispatch(self, consumer, handler, message, max_attempts=3):
        try:
            payload = decode_event(message.body)
        except InvalidEvent:
            log.warning("[%s] invalid message -> DLQ", consumer)
            await message.reject(requeue=False)
            return
        with correlation_scope(event_correlation_id(payload)):
            await self._dispatch_event(consumer, handler, message, payload, max_attempts)

    async def _dispatch_event(self, consumer, handler, message, payload, max_attempts):
        fields = event_log_fields(payload)
        try:
            if payload["event_type"] not in {EVENT_TYPES[key] for key in BINDINGS[consumer]}:
                raise InvalidEvent("Событие не поддерживается обработчиком")
            await asyncio.to_thread(handler, payload)
        except InvalidEvent:
            log.warning("[%s] invalid event -> DLQ", consumer, extra=fields)
            await message.reject(requeue=False)
            return
        except Exception as error:
            headers = dict(message.headers or {})
            raw_attempt = headers.get("x-attempt", 1)
            attempt = raw_attempt if type(raw_attempt) is int and raw_attempt > 0 else 1
            if attempt >= max_attempts:
                log.warning("[%s] exhausted %s attempts (%s) -> DLQ", consumer, attempt, type(error).__name__, extra=fields)
                await message.reject(requeue=False)
                return
            headers["x-attempt"] = attempt + 1
            try:
                await self.retry.publish(
                    aio_pika.Message(
                        body=message.body, headers=headers, content_type=message.content_type,
                        message_id=message.message_id, correlation_id=event_correlation_id(payload),
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    ), routing_key=consumer, mandatory=True, timeout=5,
                )
            except Exception:
                # Исходное сообщение нельзя подтвердить, если копия не сохранена.
                await message.nack(requeue=True)
                return
            log.warning("[%s] retry %s/%s (%s)", consumer, attempt + 1, max_attempts, type(error).__name__, extra=fields)
        await message.ack()


async def consume_forever(url, consumer, handler):
    configure_json_logging(consumer)
    while True:
        try:
            async with EventBus(url) as bus:
                await bus.queues[consumer].consume(
                    lambda message: bus.dispatch(consumer, handler, message),
                )
                log.info("[%s] waiting for events", consumer)
                await asyncio.Future()
        except (OSError, aio_pika.AMQPException, asyncio.TimeoutError) as error:
            log.warning("[%s] reconnect (%s)", consumer, type(error).__name__)
            await asyncio.sleep(3)
