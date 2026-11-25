"""Outbox работает даже после перезапуска: неподтверждённые события остаются в БД."""
import asyncio
import logging
from app.core.config import settings
from app.database import SessionLocal
from event_bus.observability import configure_json_logging
from event_bus.rabbitmq import EventBus
from event_bus.storage import publish_batch
from app.modules.search.synchronization import periodic_menu_projection


async def publish_forever():
    while True:
        try:
            async with EventBus(settings.RABBITMQ_URL) as bus:
                while True:
                    count = await publish_batch(SessionLocal, bus)
                    if count:
                        logging.info("[Outbox] published=%s", count)
                    await asyncio.sleep(0.5)
        except Exception as error:
            # Не выводим URL брокера/БД: он может содержать пароль.
            logging.warning("[Outbox] unavailable (%s); retry in 3s", type(error).__name__)
            await asyncio.sleep(3)


async def main():
    configure_json_logging("outbox")
    # Search failure/reindex/lock contention cannot delay broker publication.
    await asyncio.gather(publish_forever(), periodic_menu_projection())


if __name__ == "__main__":
    asyncio.run(main())
