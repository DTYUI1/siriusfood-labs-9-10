"""
Приложение Celery. Брокер (очередь задач) и хранилище результатов — Redis.

    celery -A app.core.celery_app worker -Q default --loglevel=info
    celery -A app.core.celery_app beat --loglevel=info      # один экземпляр на систему
    celery -A app.core.celery_app flower --address=127.0.0.1 --port=5555
"""
from celery import Celery
from celery.schedules import crontab
from kombu import Exchange, Queue

from app.core.config import settings

celery_app = Celery(
    "sirius_food",
    broker=settings.CELERY_BROKER_URL or settings.REDIS_URL,
    backend=settings.CELERY_RESULT_BACKEND or settings.REDIS_URL,
    include=["app.tasks.orders", "app.tasks.maintenance"],
)

celery_app.conf.update(
    # JSON вместо pickle: pickle из очереди позволяет выполнить произвольный код.
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_queues=(Queue("default", Exchange("default"), routing_key="default"),),
    task_default_queue="default",
    task_default_exchange="default",
    task_default_routing_key="default",
    # Сообщение подтверждается после выполнения задачи, а не при получении:
    # если воркер упадёт посреди задачи, Redis вернёт её в очередь через
    # visibility_timeout. Поэтому задачи обязаны быть идемпотентными.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": 3600},
    # Результаты задач никто не читает (Flower берёт их из событий). Без этого
    # .delay() подписывается на результат в Redis, и при недоступном Redis
    # retry policy хранилища результатов (20 попыток по 1 с) держит запрос клиента 19 секунд.
    task_ignore_result=True,
    result_expires=3600,
    # Если брокер недоступен, постановка задачи не держит запрос клиента дольше пары секунд.
    broker_connection_timeout=2,
    task_publish_retry_policy={"max_retries": 2, "interval_start": 0, "interval_step": 0.2, "interval_max": 0.5},
    # События задач для Flower: получена, начата, ретрай, успех, отказ — в реальном времени.
    worker_send_task_events=True,
    task_send_sent_event=True,
)

# Celery Beat — планировщик: сам задачи не выполняет, а по расписанию ставит их
# в ту же очередь, откуда их берут воркеры. Запускается в одном экземпляре:
# два процесса beat поставили бы каждую задачу дважды.
celery_app.conf.beat_schedule = {
    "update-order-stats": {
        "task": "app.tasks.maintenance.update_order_stats",
        "schedule": 60.0,
        # Если воркеры лежали, устаревшие запуски не копятся: каждый новый всё равно пересчитает сутки.
        "options": {"expires": 50},
    },
    "cleanup-expired-refresh-tokens": {
        "task": "app.tasks.maintenance.cleanup_expired_refresh_tokens",
        "schedule": crontab(minute=0),
    },
}
