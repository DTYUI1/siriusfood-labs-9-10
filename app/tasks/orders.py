"""
Фоновые задачи заказа: PDF-чек, письмо, SMS. Выполняются воркером Celery,
клиент не ждёт их завершения.

Каждая задача:
- повторяется при ExternalServiceError с экспоненциальной задержкой
  (1, 2, 4 с, со случайным разбросом), не больше 3 ретраев;
- после последней неудачи попадает в DLQ (DeadLetterTask.on_failure);
- идемпотентна: повторный запуск не создаёт дубль (retry_when_locked → run_once).
"""
from app.core.celery_app import celery_app
from app.notifications import gateways
from app.tasks.dlq import DeadLetterTask
from app.tasks.idempotency import retry_when_locked

RETRY_POLICY = {
    # Повторяются только сбои внешних сервисов. Ошибка в коде (TypeError и т.п.)
    # от повтора не исчезнет — такая задача сразу уходит в DLQ.
    "autoretry_for": (gateways.ExternalServiceError,),
    "retry_backoff": True,
    "retry_backoff_max": 600,
    # Случайный разброс: после сбоя сотни задач не повторятся в одну и ту же секунду.
    "retry_jitter": True,
    "max_retries": 3,
}


@celery_app.task(base=DeadLetterTask, bind=True, **RETRY_POLICY)
def generate_order_pdf(self, order_id: int) -> str:
    """Генерация PDF-чека для заказа."""
    return retry_when_locked(self, "pdf", order_id, lambda: gateways.render_receipt_pdf(order_id))


@celery_app.task(base=DeadLetterTask, bind=True, **RETRY_POLICY)
def send_order_email(self, order_id: int, email: str) -> str:
    """Отправка письма о заказе."""

    def send() -> str:
        gateways.send_email(order_id, email)
        return f"Письмо о заказе {order_id} отправлено"

    return retry_when_locked(self, "email", order_id, send)


@celery_app.task(base=DeadLetterTask, bind=True, **RETRY_POLICY)
def send_order_sms(self, order_id: int, phone: str) -> str:
    """Отправка SMS о заказе."""

    def send() -> str:
        gateways.send_sms(order_id, phone)
        return f"SMS о заказе {order_id} отправлено"

    return retry_when_locked(self, "sms", order_id, send)
