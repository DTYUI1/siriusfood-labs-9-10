"""
Слой бизнес-логики модуля orders (круги 1–2: Entities и Use Cases).
Импорты fastapi, sqlalchemy, redis и celery в этом файле запрещены.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.identity import CurrentUser
from app.exceptions import AccessDeniedError, BusinessRuleError, NotFoundError

if TYPE_CHECKING:
    # Только для аннотаций: во время выполнения сервис не загружает SQLAlchemy.
    from app.modules.orders.repository import OrderRepository

# Заказ в этом статусе кухня ещё не отдала: его можно отменить с возвратом денег.
STATUS_PROCESSING = "processing"


@dataclass(frozen=True)
class OrderCreated:
    """Событие «заказ оформлен»: всё, что нужно для чека и уведомлений."""

    order_id: int
    user_id: int
    email: str
    phone: str | None
    total_cost: float


class OrderNotifier(ABC):
    """
    Порт уведомлений о заказе. Сервис знает только, что уведомить нужно;
    чек, письмо и SMS выполняет реализация из app/notifications.
    """

    @abstractmethod
    def order_created(self, event: OrderCreated) -> None:
        """Вызывается после сохранения заказа."""


class OrderService:
    """
    Класс инкапсулирует бизнес-правила обработки заказов.
    Не имеет зависимостей от инфраструктурных фреймворков.
    """

    def __init__(self, repository: "OrderRepository", notifier: OrderNotifier | None = None):
        # Внедрение зависимостей через конструктор. notifier=None — без уведомлений
        # (сценарии, где заказ не создаётся: отмена, очередь кухни).
        self.repository = repository
        self.notifier = notifier

    def create_order(self, user_id: int, item_id: int, quantity: int, recipient: dict | None = None) -> int:
        """
        Исполнение сценария оформления заказа.
        user_id — идентификатор из JWT, а не из тела запроса.
        Возвращает идентификатор созданного заказа.
        Возбуждает DomainError при нарушении бизнес-правил.
        """
        user = self.repository.get_user_by_id(user_id)
        if not user:
            raise NotFoundError("Пользователь не найден")

        item = self.repository.get_menu_item_by_id(item_id)
        if not item:
            raise NotFoundError("Блюдо не найдено")

        if not item.is_available:
            raise BusinessRuleError("Блюдо недоступно")

        total_cost = item.price * quantity
        if user.balance < total_cost:
            raise BusinessRuleError("Недостаточно средств на балансе студента")

        # Делегирование списания и сохранения инфраструктурному слою.
        order_id = self.repository.save_order(
            user_id=user_id,
            item_id=item_id,
            quantity=quantity,
            new_balance=user.balance - total_cost,
            total_cost=total_cost,
            **({"recipient": recipient} if recipient is not None else {}),
        )

        # Бизнес-правило: после оплаты клиент получает чек и уведомления.
        # Вызов идёт после commit: заказ уже сохранён, когда за него возьмутся.
        if self.notifier is not None:
            self.notifier.order_created(
                OrderCreated(
                    order_id=order_id,
                    user_id=user.id,
                    email=user.email,
                    phone=user.phone,
                    total_cost=total_cost,
                )
            )
        return order_id

    def pay_order(self, order_id: int, current_user: CurrentUser) -> None:
        """Подтверждает уже списанную при создании оплату и сохраняет событие."""
        order = self.repository.get_order(order_id)
        if order is None:
            raise NotFoundError("Заказ не найден")
        if order.user_id != current_user.id:
            raise AccessDeniedError("Нельзя оплатить чужой заказ")
        if order.status == "paid":
            return
        if order.status != STATUS_PROCESSING or not self.repository.pay_order(order_id):
            raise BusinessRuleError("Оплата недоступна в текущем статусе заказа")

    def cancel_order(self, order_id: int, current_user: CurrentUser) -> None:
        """
        Отмена заказа с возвратом списанной суммы.
        Защита от IDOR: заказ можно отменить, только если он принадлежит
        текущему пользователю (администратор может отменить любой).
        Возбуждает NotFoundError, AccessDeniedError или BusinessRuleError.
        """
        order = self.repository.get_order(order_id)
        if order is None:
            raise NotFoundError("Заказ не найден")

        if order.user_id != current_user.id and not current_user.is_admin:
            raise AccessDeniedError("Нельзя отменить чужой заказ")

        if order.status != STATUS_PROCESSING:
            raise BusinessRuleError("Отменить можно только заказ, который ещё готовится")

        cancelled = self.repository.cancel_order(
            order_id=order.id,
            expected_status=order.status,
            user_id=order.user_id,
            refund=order.total_cost,
        )
        if not cancelled:
            raise BusinessRuleError("Отменить можно только заказ, который ещё готовится")

    def get_kitchen_orders(self) -> list:
        """Очередь кухни: заказы, которые ещё готовятся."""
        return (self.repository.get_orders_by_status(STATUS_PROCESSING)
                + self.repository.get_orders_by_status("paid"))

    def advance_kitchen(self, order_id, status):
        return self.repository.advance_kitchen(order_id, status)

    def own_orders(self, current_user: CurrentUser) -> list:
        return self.repository.own_orders(current_user.id)

    def admin_orders(self, current_user: CurrentUser, offset: int, limit: int) -> list:
        if not current_user.is_admin:
            raise AccessDeniedError("Доступно только администратору")
        return self.repository.list_orders(offset, limit)
