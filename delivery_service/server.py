"""
gRPC-сервер микросервиса доставки (транспортный слой сервиса).

Servicer переводит вызовы в сценарии DeliveryDomain, а исключения правил —
в статусы gRPC. Каждый вызов проверяется перехватчиком служебного токена.

    python delivery_server.py
"""
import hmac
import logging
import queue
import signal
import threading
import time
from collections.abc import Callable
from concurrent import futures
from contextlib import contextmanager

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

import delivery_pb2
import delivery_pb2_grpc
from delivery_service.chat import CLOSED, ChatHub
from delivery_service.domain import (
    FINAL_STATUSES,
    DeliveryDomain,
    DeliveryError,
    DeliveryRecord,
    Forbidden,
    NotFound,
    RuleViolation,
    TransitionConflict,
)
from delivery_service.repository import SqlAlchemyDeliveryRepository

logger = logging.getLogger("delivery_service")

_STATUS_CODES = {
    NotFound: grpc.StatusCode.NOT_FOUND,
    TransitionConflict: grpc.StatusCode.ABORTED,
    RuleViolation: grpc.StatusCode.FAILED_PRECONDITION,
    Forbidden: grpc.StatusCode.PERMISSION_DENIED,
}
MAX_CHAT_MESSAGE_LENGTH = 1000


def _now() -> Timestamp:
    timestamp = Timestamp()
    timestamp.GetCurrentTime()
    return timestamp


def _to_message(delivery: DeliveryRecord) -> delivery_pb2.Delivery:
    return delivery_pb2.Delivery(
        id=delivery.id,
        order_id=delivery.order_id,
        courier_id=delivery.courier_id or 0,
        customer_id=delivery.customer_id or 0,
        status=delivery.status,
    )


class DeliveryServiceServicer(delivery_pb2_grpc.DeliveryServiceServicer):
    """Реализация контракта proto/delivery.proto."""

    def __init__(self, session_factory: Callable, hub: ChatHub, poll_seconds: float, max_track_seconds: float):
        self.session_factory = session_factory
        self.hub = hub
        self.poll_seconds = poll_seconds
        self.max_track_seconds = max_track_seconds

    @contextmanager
    def _domain(self, context):
        """Сессия БД на один вызов; исключение правила → статус gRPC с текстом ошибки."""
        with self.session_factory() as db:
            try:
                yield DeliveryDomain(SqlAlchemyDeliveryRepository(db))
            except DeliveryError as error:
                context.abort(_STATUS_CODES[type(error)], str(error))

    def AssignCourier(self, request, context):
        with self._domain(context) as domain:
            delivery = domain.assign_courier(request.order_id, request.courier_id)
        logger.info("AssignCourier: заказ %s, курьер %s → доставка %s", request.order_id, request.courier_id, delivery.id)
        return delivery_pb2.AssignCourierResponse(delivery_id=delivery.id, status=delivery.status)

    def PickUpOrder(self, request, context):
        with self._domain(context) as domain:
            status = domain.pick_up(request.delivery_id)
        logger.info("PickUpOrder: доставка %s → %s", request.delivery_id, status)
        return delivery_pb2.PickUpOrderResponse(status=status)

    def CompleteDelivery(self, request, context):
        with self._domain(context) as domain:
            status = domain.complete(request.delivery_id)
        logger.info("CompleteDelivery: доставка %s → %s", request.delivery_id, status)
        return delivery_pb2.CompleteDeliveryResponse(status=status)

    def GetDelivery(self, request, context):
        with self._domain(context) as domain:
            delivery = domain.get(request.delivery_id)
        return delivery_pb2.GetDeliveryResponse(delivery=_to_message(delivery))

    def ListCourierDeliveries(self, request, context):
        with self._domain(context) as domain:
            deliveries = domain.courier_deliveries(request.courier_id)
        return delivery_pb2.ListCourierDeliveriesResponse(deliveries=[_to_message(d) for d in deliveries])

    def ListDeliveries(self, request, context):
        with self._domain(context) as domain:
            deliveries = domain.list_deliveries(request.offset, request.limit)
        return delivery_pb2.ListDeliveriesResponse(deliveries=[_to_message(d) for d in deliveries])

    def TrackDelivery(self, request, context):
        """
        Server Streaming: текущий статус сразу, затем каждое изменение.
        Статус перечитывается раз в poll_seconds. Цикл прекращается, если клиент
        отключился (context.is_active), доставка в конечном статусе или
        поток открыт дольше max_track_seconds.
        """
        logger.info("TrackDelivery: доставка %s, подписка", request.delivery_id)
        deadline = time.monotonic() + self.max_track_seconds
        last_status = None
        while context.is_active():
            with self._domain(context) as domain:
                status = domain.get(request.delivery_id).status
            if status != last_status:
                last_status = status
                final = status in FINAL_STATUSES
                yield delivery_pb2.DeliveryStatus(
                    delivery_id=request.delivery_id, status=status, observed_at=_now(), final=final
                )
                if final:
                    return
            if time.monotonic() >= deadline:
                return
            time.sleep(self.poll_seconds)

    def ChatWithCourier(self, request_iterator, context):
        """
        Bidirectional Streaming. Входящие сообщения читает отдельный поток и
        рассылает в комнату доставки; генератор отдаёт клиенту историю и новые
        сообщения обоих участников. Клиент закрыл свою сторону — поток закрывается.
        """
        metadata = dict(context.invocation_metadata())
        try:
            delivery_id = int(metadata["x-delivery-id"])
            user_id = int(metadata["x-user-id"])
        except (KeyError, ValueError):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Нужны метаданные x-delivery-id и x-user-id")

        with self._domain(context) as domain:
            role = domain.chat_role(delivery_id, user_id)
        logger.info("ChatWithCourier: доставка %s, вошёл %s %s", delivery_id, role, user_id)

        inbox, history = self.hub.join(delivery_id)

        def receive() -> None:
            try:
                for message in request_iterator:
                    text = message.text.strip()[:MAX_CHAT_MESSAGE_LENGTH]
                    if text:
                        self.hub.publish(
                            delivery_id,
                            delivery_pb2.ChatMessage(text=text, sender_id=user_id, sender_role=role, sent_at=_now()),
                        )
            except grpc.RpcError:
                pass  # клиент оборвал соединение
            finally:
                inbox.put(CLOSED)

        threading.Thread(target=receive, name=f"chat-{delivery_id}-{user_id}", daemon=True).start()
        try:
            yield from history
            while context.is_active():
                try:
                    message = inbox.get(timeout=0.5)
                except queue.Empty:
                    continue
                if message is CLOSED:
                    return
                yield message
        finally:
            self.hub.leave(delivery_id, inbox)
            logger.info("ChatWithCourier: доставка %s, вышел %s %s", delivery_id, role, user_id)


class TokenAuthInterceptor(grpc.ServerInterceptor):
    """Пропускает только вызовы с метаданными authorization: Bearer <служебный токен>."""

    def __init__(self, token: str):
        self._expected = f"Bearer {token}".encode()

    def intercept_service(self, continuation, handler_call_details):
        metadata = dict(handler_call_details.invocation_metadata)
        # compare_digest: время сравнения не зависит от того, сколько символов совпало.
        if hmac.compare_digest(metadata.get("authorization", "").encode(), self._expected):
            return continuation(handler_call_details)

        handler = continuation(handler_call_details)
        if handler is None:
            return None

        def deny(_request, context):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Нужен служебный токен")

        return _handler_like(handler, deny)


def _handler_like(handler, behavior):
    """Обработчик того же вида (unary/stream), что и handler, но с другим поведением."""
    if handler.request_streaming and handler.response_streaming:
        factory = grpc.stream_stream_rpc_method_handler
    elif handler.request_streaming:
        factory = grpc.stream_unary_rpc_method_handler
    elif handler.response_streaming:
        factory = grpc.unary_stream_rpc_method_handler
    else:
        factory = grpc.unary_unary_rpc_method_handler
    return factory(
        behavior,
        request_deserializer=handler.request_deserializer,
        response_serializer=handler.response_serializer,
    )


def create_server(
    session_factory: Callable,
    token: str,
    bind: str,
    workers: int = 32,
    poll_seconds: float = 1.0,
    max_track_seconds: float = 3600,
) -> tuple[grpc.Server, int]:
    """Собирает сервер (ещё не запущенный). Возвращает его и фактический порт (для bind с портом 0)."""
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="delivery-rpc"),
        interceptors=[TokenAuthInterceptor(token)],
        # Каждый открытый поток трекинга или чата держит поток пула. Сверх лимита
        # клиент сразу получает RESOURCE_EXHAUSTED, а не ждёт в очереди за потоками.
        maximum_concurrent_rpcs=workers,
        # По умолчанию gRPC включает SO_REUSEPORT: второй запущенный экземпляр молча
        # слушает тот же порт, и ядро делит соединения между процессами. Если один
        # из них завис или останавливается, часть вызовов монолита получает UNAVAILABLE.
        # Без SO_REUSEPORT второй экземпляр сразу падает с ошибкой «порт занят».
        options=[("grpc.so_reuseport", 0)],
    )
    servicer = DeliveryServiceServicer(session_factory, ChatHub(), poll_seconds, max_track_seconds)
    delivery_pb2_grpc.add_DeliveryServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port(bind)
    return server, port


def serve() -> None:
    """Запуск микросервиса как отдельного процесса."""
    from delivery_service.config import settings
    from delivery_service.database import SessionLocal

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server, _ = create_server(
        SessionLocal,
        token=settings.DELIVERY_GRPC_TOKEN,
        bind=settings.DELIVERY_GRPC_BIND,
        workers=settings.DELIVERY_GRPC_WORKERS,
        poll_seconds=settings.TRACK_POLL_SECONDS,
        max_track_seconds=settings.TRACK_MAX_SECONDS,
    )
    server.start()
    logger.info("Delivery gRPC Server running on %s", settings.DELIVERY_GRPC_BIND)

    # SIGTERM (docker stop, systemd): новые вызовы не принимаются, начатым даётся 5 секунд.
    signal.signal(signal.SIGTERM, lambda *_: server.stop(grace=5))
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        server.stop(grace=5)
