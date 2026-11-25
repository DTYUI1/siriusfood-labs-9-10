"""Ручная публикация для демонстрации. Оплата использует transactional outbox."""
import aio_pika
from app.core.config import settings
from event_bus.rabbitmq import EventBus

EXCHANGE_NAME = "sirius_events"


async def get_connection():
    return await aio_pika.connect_robust(settings.RABBITMQ_URL, timeout=5)


async def publish_event(routing_key, payload):
    async with EventBus(settings.RABBITMQ_URL) as bus:
        await bus.publish_event(routing_key, payload)
