"""
Реализации порта OrderNotifier (круг 3: адаптеры).
OrderService решает, что после заказа нужно уведомить клиента;
как именно — синхронно или через очередь — решают классы этого модуля.
"""
import logging

from kombu.exceptions import KombuError

from app.core.config import settings
from app.modules.orders.service import OrderCreated, OrderNotifier
from app.notifications import gateways

logger = logging.getLogger(__name__)


class SyncOrderNotifier(OrderNotifier):
    """
    Чек, письмо и SMS выполняются прямо в обработчике запроса:
    клиент ждёт около 4 секунд. Режим для замера «до».
    """

    def order_created(self, event: OrderCreated) -> None:
        gateways.render_receipt_pdf(event.order_id)
        try:
            gateways.send_email(event.order_id, event.email)
        except gateways.ExternalServiceError as error:
            # Заказ уже оплачен и сохранён: ошибка почты не должна превращаться в 500.
            logger.error("Письмо о заказе %s не отправлено: %s", event.order_id, error)
        if event.phone:
            gateways.send_sms(event.order_id, event.phone)


class CeleryOrderNotifier(OrderNotifier):
    """
    Ставит задачи в очередь Celery и сразу возвращает управление: .delay()
    только записывает сообщение в Redis, работу выполняет воркер.
    """

    def order_created(self, event: OrderCreated) -> None:
        # Импорт здесь: модуль задач загружает приложение Celery, а в режиме sync оно не нужно.
        from app.tasks import orders as tasks

        try:
            tasks.generate_order_pdf.delay(event.order_id)
            tasks.send_order_email.delay(event.order_id, event.email)
            if event.phone:
                tasks.send_order_sms.delay(event.order_id, event.phone)
        except (KombuError, OSError) as error:
            # Брокер недоступен. Заказ уже оплачен и сохранён: ответ 500 заставил бы
            # клиента повторить запрос и заплатить дважды. Поэтому ошибка логируется.
            logger.error("Уведомления о заказе %s не поставлены в очередь: %s", event.order_id, error)


def get_order_notifier() -> OrderNotifier:
    """Реализация уведомлений по настройке ORDER_NOTIFICATIONS."""
    if settings.ORDER_NOTIFICATIONS == "sync":
        return SyncOrderNotifier()
    return CeleryOrderNotifier()
