"""
Dead Letter Queue: задачи, которые не выполнились после всех ретраев.

У транспорта Redis нет встроенного dead-lettering (в RabbitMQ это делает
x-dead-letter-exchange), поэтому задачу в DLQ кладёт обработчик on_failure.
DLQ — список Redis "dlq" с читаемыми JSON-записями:

    redis-cli lrange dlq 0 -1
    python -m app.cli dlq-requeue      # после починки внешнего сервиса
"""
import json
import logging
from datetime import datetime, timezone

from celery import Task

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

DLQ_KEY = "dlq"
# Записи содержат email и телефоны: DLQ не должна расти бесконечно.
DLQ_MAX_LENGTH = 1000


class DeadLetterTask(Task):
    """Базовый класс задач: окончательный отказ записывается в DLQ."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        # Вызывается, когда ретраи исчерпаны или ошибка не из autoretry_for.
        record = {
            "task": self.name,
            "task_id": task_id,
            "args": list(args),
            "kwargs": kwargs,
            "error": repr(exc),
            "retries": self.request.retries,
            "failed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        client = get_redis()
        client.lpush(DLQ_KEY, json.dumps(record, ensure_ascii=False))
        client.ltrim(DLQ_KEY, 0, DLQ_MAX_LENGTH - 1)
        logger.error("Задача %s[%s] отправлена в DLQ после %s ретраев: %r", self.name, task_id, self.request.retries, exc)


def dead_letters(limit: int = 100) -> list[dict]:
    """Последние записи DLQ, новые первыми."""
    return [json.loads(item) for item in get_redis().lrange(DLQ_KEY, 0, limit - 1)]


def requeue_dead_letters() -> int:
    """
    Повторная постановка задач из DLQ в очередь default, от старых к новым.
    Повторы безопасны: задачи идемпотентны. Возвращает число задач.
    """
    from app.core.celery_app import celery_app

    client = get_redis()
    count = 0
    while (item := client.rpop(DLQ_KEY)) is not None:
        record = json.loads(item)
        try:
            celery_app.send_task(record["task"], args=record["args"], kwargs=record["kwargs"])
        except Exception:
            client.rpush(DLQ_KEY, item)
            raise
        count += 1
    return count
