"""
Бизнес-правила доставки — то, что раньше было в app/modules/delivery/service.py.
Импорты grpc и sqlalchemy здесь запрещены: правила не знают ни о сети, ни о СУБД.

Права пользователя на конкретную доставку (IDOR) проверяет монолит: он знает,
кто делает запрос (JWT, роли). Сервис отвечает за инварианты самой доставки.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass

STATUS_IN_PROGRESS = "in_progress"
STATUS_ON_THE_WAY = "on_the_way"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
# У курьера не больше одной активной доставки.
ACTIVE_STATUSES = (STATUS_IN_PROGRESS, STATUS_ON_THE_WAY)
# После этих статусов доставка не меняется, трекинг закрывается.
FINAL_STATUSES = (STATUS_COMPLETED, STATUS_CANCELLED)

ROLE_COURIER = "courier"
ROLE_CUSTOMER = "customer"


class DeliveryError(Exception):
    """Базовое исключение правил доставки."""


class NotFound(DeliveryError):
    """Заказ, курьер или доставка не найдены."""


class RuleViolation(DeliveryError):
    """Нарушено правило: заказ не оплачен, доставка уже завершена и т.п."""


class TransitionConflict(RuleViolation):
    """New kitchen/delivery state conflict, transported without PII."""


class Forbidden(DeliveryError):
    """Действие запрещено: курьер заблокирован, пользователь не участник доставки."""


@dataclass(frozen=True)
class DeliveryRecord:
    id: int
    order_id: int
    courier_id: int | None
    customer_id: int | None
    status: str


@dataclass(frozen=True)
class OrderInfo:
    id: int
    user_id: int
    status: str


@dataclass(frozen=True)
class CourierInfo:
    id: int
    role: str
    is_blocked: bool


class DeliveryRepository(ABC):
    """Порт хранилища. Реализация на SQLAlchemy — delivery_service/repository.py."""

    @abstractmethod
    def get_order(self, order_id: int) -> OrderInfo | None: ...

    @abstractmethod
    def get_user(self, user_id: int) -> CourierInfo | None: ...

    @abstractmethod
    def get(self, delivery_id: int) -> DeliveryRecord | None: ...

    @abstractmethod
    def list_by_courier(self, courier_id: int, statuses: tuple[str, ...] | None = None) -> list[DeliveryRecord]: ...

    def list_deliveries(self, offset: int, limit: int) -> list[DeliveryRecord]:
        raise NotImplementedError

    @abstractmethod
    def add(self, order_id: int, courier_id: int, status: str) -> int: ...

    @abstractmethod
    def change_status(self, delivery_id: int, expected: tuple[str, ...], new_status: str) -> bool:
        """
        Меняет статус, только если текущий входит в expected. False — статус успели
        изменить (два одновременных «завершить»). При new_status=completed заказ
        в той же транзакции получает статус delivered.
        """


class DeliveryDomain:
    """Сценарии доставки. Каждый метод возбуждает NotFound, RuleViolation или Forbidden."""

    def __init__(self, repository: DeliveryRepository):
        self.repository = repository

    def assign_courier(self, order_id: int, courier_id: int) -> DeliveryRecord:
        order = self.repository.get_order(order_id)
        if not order:
            raise NotFound("Заказ не найден")

        # Назначить курьера можно только на оплаченный заказ.
        if order.status != "paid":
            raise RuleViolation("Курьера можно назначить только на оплаченный заказ")

        courier = self.repository.get_user(courier_id)
        if not courier:
            raise NotFound("Курьер не найден")

        if courier.role != ROLE_COURIER:
            raise RuleViolation("Пользователь не является курьером")

        if courier.is_blocked:
            raise Forbidden("Курьер заблокирован и не может быть назначен")

        # Один курьер — одна активная доставка.
        if self.repository.list_by_courier(courier_id, statuses=ACTIVE_STATUSES):
            raise RuleViolation("У курьера уже есть активная доставка")

        delivery_id = self.repository.add(order_id=order_id, courier_id=courier_id, status=STATUS_IN_PROGRESS)
        return DeliveryRecord(delivery_id, order_id, courier_id, order.user_id, STATUS_IN_PROGRESS)

    def get(self, delivery_id: int) -> DeliveryRecord:
        delivery = self.repository.get(delivery_id)
        if not delivery:
            raise NotFound("Доставка не найдена")
        return delivery

    def courier_deliveries(self, courier_id: int) -> list[DeliveryRecord]:
        return self.repository.list_by_courier(courier_id)

    def list_deliveries(self, offset: int, limit: int) -> list[DeliveryRecord]:
        if offset < 0 or not 1 <= limit <= 100:
            raise RuleViolation("Недопустимая страница")
        return self.repository.list_deliveries(offset, limit)

    def pick_up(self, delivery_id: int) -> str:
        """Курьер забрал заказ из столовой: in_progress → on_the_way."""
        delivery = self.get(delivery_id)
        if delivery.status != STATUS_IN_PROGRESS:
            raise RuleViolation(_cannot_change(delivery.status))
        self._change(delivery.id, (STATUS_IN_PROGRESS,), STATUS_ON_THE_WAY)
        return STATUS_ON_THE_WAY

    def complete(self, delivery_id: int) -> str:
        """Завершение доставки, заказ получает статус delivered."""
        delivery = self.get(delivery_id)
        if delivery.status == STATUS_COMPLETED:
            return STATUS_COMPLETED
        if delivery.status != STATUS_ON_THE_WAY:
            raise TransitionConflict("Сначала курьер должен забрать заказ")
        if not self.repository.change_status(delivery.id, (STATUS_ON_THE_WAY,), STATUS_COMPLETED):
            if self.get(delivery.id).status != STATUS_COMPLETED:
                raise RuleViolation(_cannot_change(self.get(delivery.id).status))
        return STATUS_COMPLETED

    def chat_role(self, delivery_id: int, user_id: int) -> str:
        """Роль пользователя в чате доставки. Чужим в чат нельзя."""
        delivery = self.get(delivery_id)
        if delivery.courier_id == user_id:
            return ROLE_COURIER
        if delivery.customer_id == user_id:
            return ROLE_CUSTOMER
        raise Forbidden("Чат доступен только заказчику и курьеру этой доставки")

    def _change(self, delivery_id: int, expected: tuple[str, ...], new_status: str) -> None:
        if not self.repository.change_status(delivery_id, expected, new_status):
            # Статус изменил параллельный запрос — повторяем проверку на свежих данных.
            raise RuleViolation(_cannot_change(self.get(delivery_id).status))


def _cannot_change(status: str) -> str:
    return {
        STATUS_COMPLETED: "Доставка уже завершена",
        STATUS_CANCELLED: "Отменённую доставку нельзя завершить",
        STATUS_ON_THE_WAY: "Курьер уже забрал заказ",
    }.get(status, f"Действие недоступно в статусе {status}")
