"""Идемпотентно объявляет exchange, очереди, retry и DLQ до публикации событий."""
import asyncio
from app.core.config import settings
from event_bus.rabbitmq import EventBus


async def setup():
    async with EventBus(settings.RABBITMQ_URL):
        print("RabbitMQ: exchange, queues, retry и DLQ настроены")


if __name__ == "__main__":
    asyncio.run(setup())
