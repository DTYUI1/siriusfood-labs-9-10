"""
Идемпотентность фоновых задач: повторный запуск (ретрай, повторная доставка
сообщения после падения воркера) не создаёт второй чек и не шлёт второе письмо.
"""
import logging
from collections.abc import Callable
from typing import TypeVar

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Сколько помнить о выполненной задаче. Больше visibility_timeout брокера (1 час)
# с запасом: повторная доставка сообщения позже этого срока уже не придёт.
DONE_TTL = 7 * 24 * 3600
# Блокировка на время выполнения (самая долгая задача — PDF, 2 с). Если воркер
# упал посреди задачи, блокировка истечёт сама.
LOCK_TTL = 60


class TaskInProgress(Exception):
    """Блокировку держит другой воркер — или её оставил упавший."""


def run_once(kind: str, order_id: int, action: Callable[[], T]) -> T | str:
    """
    Выполняет action один раз для пары (kind, order_id).

    done-ключ ставится только после успешного выполнения: упавшая попытка
    не помечается выполненной, и ретрай повторит работу. lock-ключ (SET NX)
    не даёт двум воркерам выполнить одну задачу одновременно — проверка
    «exists, потом setex» из методички этого не гарантирует.

    Занятая блокировка — не повод пропустить задачу: её мог оставить воркер,
    упавший посреди работы. Тогда возбуждается TaskInProgress, и задача
    повторяется после истечения блокировки (retry_when_locked).
    """
    client = get_redis()
    done_key = f"task:{kind}:{order_id}:done"
    lock_key = f"task:{kind}:{order_id}:lock"

    if client.exists(done_key):
        logger.info("%s для заказа %s уже выполнено, пропуск", kind, order_id)
        return f"{kind} для заказа {order_id} уже выполнено"
    if not client.set(lock_key, "1", nx=True, ex=LOCK_TTL):
        raise TaskInProgress(f"{kind} для заказа {order_id} уже выполняется")

    try:
        result = action()
        client.set(done_key, "1", ex=DONE_TTL)
        return result
    finally:
        client.delete(lock_key)


def retry_when_locked(task, kind: str, order_id: int, action: Callable[[], T]) -> T | str:
    """run_once внутри задачи Celery: при занятой блокировке задача откладывается на LOCK_TTL."""
    try:
        return run_once(kind, order_id, action)
    except TaskInProgress as busy:
        logger.info("%s, повтор через %s с", busy, LOCK_TTL)
        raise task.retry(exc=busy, countdown=LOCK_TTL)
