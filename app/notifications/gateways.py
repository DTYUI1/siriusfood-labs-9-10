"""
Заглушки внешних сервисов: генерация PDF-чека, почта, SMS.
time.sleep имитирует время ответа настоящего сервиса.
"""
import logging
import time

from app.core.config import settings

logger = logging.getLogger(__name__)

PDF_SECONDS = 2
EMAIL_SECONDS = 1
SMS_SECONDS = 1


class ExternalServiceError(Exception):
    """Внешний сервис временно недоступен. Такую ошибку имеет смысл повторить."""


def render_receipt_pdf(order_id: int) -> str:
    """Генерация PDF-чека. Возвращает путь к файлу."""
    time.sleep(PDF_SECONDS)
    path = f"receipts/order-{order_id}.pdf"
    logger.info("PDF-чек заказа %s: %s", order_id, path)
    return path


def send_email(order_id: int, email: str) -> None:
    """
    Письмо о заказе. Каждый EMAIL_FAIL_EVERY_NTH_ORDER-й заказ имитирует
    недоступность почтового сервиса — для демонстрации ретраев и DLQ.
    """
    time.sleep(EMAIL_SECONDS)
    every = settings.EMAIL_FAIL_EVERY_NTH_ORDER
    if every and order_id % every == 0:
        raise ExternalServiceError("Почтовый сервис недоступен")
    logger.info("Письмо о заказе %s отправлено на %s", order_id, email)


def send_sms(order_id: int, phone: str) -> None:
    """SMS о заказе."""
    time.sleep(SMS_SECONDS)
    logger.info("SMS о заказе %s отправлено на %s", order_id, phone)
