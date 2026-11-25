"""
Клиент Redis. Используется для кеша (репозитории) и rate limiting.
"""
import redis

from app.core.config import settings

# decode_responses=True: из Redis приходят строки, а не байты — удобно для JSON.
# Короткие таймауты: если Redis завис, запрос не ждёт его десятки секунд.
redis_client = redis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    socket_timeout=1,
    socket_connect_timeout=1,
)


def get_redis() -> redis.Redis:
    """Клиент Redis для Depends. Тесты подменяют redis_client на fakeredis."""
    return redis_client


def get_cache() -> redis.Redis | None:
    """Клиент Redis для кеша репозиториев. None — кеш выключен (CACHE_ENABLED=false)."""
    return redis_client if settings.CACHE_ENABLED else None


def check_redis_connection() -> bool:
    """Проверка соединения с Redis."""
    try:
        return bool(redis_client.ping())
    except redis.RedisError:
        return False
