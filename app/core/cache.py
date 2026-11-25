"""
Стратегия Cache-Aside поверх Redis для инфраструктурного слоя.

Кешем пользуются только репозитории. Сервисы этот модуль не импортируют:
бизнес-логике всё равно, пришли данные из PostgreSQL или из Redis.
"""
import json
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from redis import Redis, RedisError
from redis.exceptions import LockError

from app.core.observability import CACHE_READS

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CacheAside:
    """
    Чтение: ищем в кеше → hit — отдаём → miss — читаем из БД, кладём в кеш с TTL.
    Запись: после изменения данных в БД ключи удаляются (событийная инвалидация).

    Защита от cache stampede: когда ключ истёк под нагрузкой, в БД идёт только
    один запрос — тот, что взял блокировку lock:{key}. Остальные ждут её и
    после этого находят значение в кеше.

    Кеш — ускоритель, а не источник данных: если Redis недоступен, данные
    читаются из БД (fail open). Приложение работает медленнее, но не падает.
    """

    def __init__(self, client: Redis | None, lock_timeout: float = 5, wait_timeout: float = 3):
        # client=None — кеш выключен, все чтения идут в БД.
        self.client = client
        # Через lock_timeout блокировка снимается сама, даже если её владелец упал.
        self.lock_timeout = lock_timeout
        # Дольше wait_timeout запрос не ждёт: читает из БД сам, не трогая кеш.
        self.wait_timeout = wait_timeout

    def get_or_load(
        self,
        key: str,
        ttl: int,
        load: Callable[[], T],
        dump: Callable[[T], Any],
        restore: Callable[[Any], T],
        empty_ttl: int | None = None,
    ) -> T:
        """
        Значение по ключу. load — чтение из БД, dump/restore — преобразование
        значения в JSON-совместимый вид и обратно. Если load вернул None,
        результат кешируется на empty_ttl секунд (None — не кешируется).
        """
        cached = self._get(key)
        if cached is not None:
            return restore(json.loads(cached))
        if self.client is None:
            return load()

        try:
            lock = self.client.lock(f"lock:{key}", timeout=self.lock_timeout, blocking_timeout=self.wait_timeout)
            acquired = lock.acquire()
        except RedisError as error:
            logger.warning("Redis недоступен, чтение %s из БД: %s", key, error)
            return load()
        if not acquired:
            return load()

        try:
            # Повторная проверка: пока запрос ждал блокировку, кеш мог
            # заполнить тот, кто взял её первым.
            cached = self._get(key)
            if cached is not None:
                return restore(json.loads(cached))
            value = load()
            if value is not None:
                self.put(key, ttl, dump(value))
            elif empty_ttl is not None:
                self.put(key, empty_ttl, None)
            return value
        finally:
            try:
                lock.release()
            except (LockError, RedisError):
                # Блокировка уже истекла (загрузка шла дольше lock_timeout) — не ошибка.
                pass

    def put(self, key: str, ttl: int, data: Any) -> None:
        """Запись готового JSON-совместимого значения (прогрев кеша)."""
        self._set(key, ttl, json.dumps(data, ensure_ascii=False))

    def invalidate(self, *keys: str) -> None:
        """
        Удаление ключей после записи в БД. Если Redis недоступен, устаревшие
        данные проживут до истечения TTL — поэтому TTL нужен и при событийной инвалидации.
        """
        if self.client is None or not keys:
            return
        try:
            self.client.delete(*keys)
        except RedisError as error:
            logger.warning("Не удалось сбросить кеш %s: %s", keys, error)

    def _get(self, key: str) -> str | None:
        if self.client is None:
            return None
        try:
            value = self.client.get(key)
        except RedisError as error:
            CACHE_READS.labels("error").inc()
            logger.warning("Redis недоступен, чтение %s из БД: %s", key, error)
            return None
        CACHE_READS.labels("hit" if value is not None else "miss").inc()
        return value

    def _set(self, key: str, ttl: int, value: str) -> None:
        if self.client is None:
            return
        try:
            self.client.setex(key, ttl, value)
        except RedisError as error:
            logger.warning("Не удалось записать %s в кеш: %s", key, error)
