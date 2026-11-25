"""
Периодические задачи (Celery Beat): статистика заказов и очистка таблицы refresh_tokens.
"""
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.jwt import utc_now
from app.core.redis import get_redis
from app.database import SessionLocal
from app.modules.auth.repository import SqlAlchemyRefreshTokenRepository
from app.modules.orders.repository import OrderRepository
from app.tasks.dlq import DeadLetterTask

logger = logging.getLogger(__name__)

# Статистику за день храним неделю.
STATS_TTL = 8 * 24 * 3600


def stats_key(day) -> str:
    return f"stats:orders:{day.isoformat()}"


def day_bounds_utc(now_utc: datetime) -> tuple:
    """Начало и конец текущих суток в STATS_TIMEZONE, переведённые в UTC без часового пояса."""
    zone = ZoneInfo(settings.STATS_TIMEZONE)
    local_day = now_utc.replace(tzinfo=timezone.utc).astimezone(zone).date()
    start = datetime.combine(local_day, time.min, tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    return local_day, start, start + timedelta(days=1)


@celery_app.task(base=DeadLetterTask)
def update_order_stats() -> dict:
    """
    Раз в минуту: заказы за текущие сутки по статусам и выручка — в хеш Redis
    stats:orders:YYYY-MM-DD. Задача идемпотентна сама по себе: она каждый раз
    пересчитывает сутки целиком, а не прибавляет к счётчику.
    """
    day, since, until = day_bounds_utc(utc_now())
    with SessionLocal() as db:
        stats = OrderRepository(db).get_stats(since, until)

    key = stats_key(day)
    client = get_redis()
    client.delete(key)
    client.hset(
        key,
        mapping={
            "total": stats["total"],
            "revenue": stats["revenue"],
            **{f"status:{status}": count for status, count in stats["by_status"].items()},
            "updated_at": utc_now().isoformat(timespec="seconds"),
        },
    )
    client.expire(key, STATS_TTL)
    logger.info("Статистика за %s: %s заказов, выручка %s", day, stats["total"], stats["revenue"])
    return {"day": day.isoformat(), **stats}


@celery_app.task(base=DeadLetterTask)
def cleanup_expired_refresh_tokens() -> int:
    """Раз в час: удаление истёкших refresh-токенов, иначе таблица растёт с каждым входом."""
    with SessionLocal() as db:
        deleted = SqlAlchemyRefreshTokenRepository(db).delete_expired(utc_now())
    logger.info("Удалено истёкших refresh-токенов: %s", deleted)
    return deleted
