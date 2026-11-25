"""
Слой бизнес-логики модуля delivery монолита (круги 1–2).
Импорты fastapi, sqlalchemy, redis, celery и grpc в этом файле запрещены.

Strangler Fig: правила доставки (оплаченный заказ, одна активная доставка,
переходы статусов) переехали в микросервис delivery_service. Монолит
по-прежнему отвечает за то, кто делает запрос и что ему можно: проверка прав
на конкретную доставку (IDOR) осталась здесь. Сервис работает с портом
DeliveryGateway и не знает, что за ним — gRPC и отдельный процесс.
"""
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from app.core.identity import CurrentUser
from app.exceptions import AccessDeniedError


@dataclass(frozen=True)
class DeliveryInfo:
    id: int
    order_id: int
    courier_id: int | None
    customer_id: int | None
    status: str


@dataclass(frozen=True)
class DeliveryStatusUpdate:
    delivery_id: int
    status: str
    observed_at: datetime
    final: bool


@dataclass(frozen=True)
class ChatLine:
    sender_id: int
    sender_role: str
    text: str
    sent_at: datetime


class DeliveryGateway(ABC):
    """
    Порт микросервиса доставки. Реализация — DeliveryGrpcClient (grpc_client.py).
    Ошибки — доменные исключения: NotFoundError, BusinessRuleError,
    OperationForbiddenError, ServiceUnavailableError, ServiceTimeoutError.
    """

    @abstractmethod
    def assign_courier(self, order_id: int, courier_id: int) -> int: ...

    @abstractmethod
    def get_delivery(self, delivery_id: int) -> DeliveryInfo: ...

    @abstractmethod
    def courier_deliveries(self, courier_id: int) -> list[DeliveryInfo]: ...

    def list_deliveries(self, offset: int, limit: int) -> list[DeliveryInfo]:
        raise NotImplementedError

    @abstractmethod
    def pick_up(self, delivery_id: int) -> None: ...

    @abstractmethod
    def complete(self, delivery_id: int) -> None: ...

    @abstractmethod
    def track(self, delivery_id: int) -> Iterator[DeliveryStatusUpdate]:
        """Текущий статус и все следующие изменения до конечного статуса."""

    @abstractmethod
    def chat(self, delivery_id: int, user_id: int, outgoing: Iterator[str]) -> Iterator[ChatLine]:
        """Отправляет сообщения из outgoing и возвращает поток сообщений чата."""


class DeliveryService:
    """
    Сценарии доставки с проверкой прав: курьер работает только со своими
    доставками, студент видит только доставку своего заказа, администратор — любые.
    """

    def __init__(self, gateway: DeliveryGateway, read_repository=None):
        self.gateway = gateway
        self.read_repository = read_repository

    def view(self, delivery, current_user):
        return self.read_repository.view(delivery, current_user)

    def assign_courier(self, order_id: int, courier_id: int) -> int:
        """Назначение курьера. Кто может назначать, проверяет роутер (cook, admin)."""
        return self.gateway.assign_courier(order_id=order_id, courier_id=courier_id)

    def get_delivery(self, delivery_id: int, current_user: CurrentUser) -> DeliveryInfo:
        """Доставка по id. Защита от IDOR: назначенный курьер, заказчик, администратор."""
        delivery = self.gateway.get_delivery(delivery_id)
        is_courier = delivery.courier_id == current_user.id
        is_customer = delivery.customer_id == current_user.id
        if not (is_courier or is_customer or current_user.is_admin):
            raise AccessDeniedError("Нет доступа к этой доставке")
        return delivery

    def get_courier_deliveries(self, current_user: CurrentUser) -> list[DeliveryInfo]:
        """Доставки текущего курьера. id берётся из токена, а не из запроса."""
        return self.gateway.courier_deliveries(current_user.id)

    def pick_up(self, delivery_id: int, current_user: CurrentUser) -> None:
        """Курьер забрал заказ. Только назначенный курьер (или администратор)."""
        self._ensure_courier(delivery_id, current_user, "Нельзя забрать заказ другого курьера")
        self.gateway.pick_up(delivery_id)

    def complete_delivery(self, delivery_id: int, current_user: CurrentUser) -> None:
        """Завершение доставки. Только назначенный курьер (или администратор)."""
        self._ensure_courier(delivery_id, current_user, "Нельзя завершить доставку другого курьера")
        self.gateway.complete(delivery_id)

    def track(self, delivery_id: int, current_user: CurrentUser) -> Iterator[DeliveryStatusUpdate]:
        """Трекинг доставки — тем же, кому доступна сама доставка."""
        self.get_delivery(delivery_id, current_user)
        return self.gateway.track(delivery_id)

    def chat(self, delivery_id: int, current_user: CurrentUser, outgoing: Iterator[str]) -> Iterator[ChatLine]:
        """Чат заказчика и курьера. Администратор в чат не входит: это переписка двоих."""
        delivery = self.gateway.get_delivery(delivery_id)
        if current_user.id not in (delivery.courier_id, delivery.customer_id):
            raise AccessDeniedError("Чат доступен только заказчику и курьеру этой доставки")
        return self.gateway.chat(delivery_id, current_user.id, outgoing)

    def _ensure_courier(self, delivery_id: int, current_user: CurrentUser, message: str) -> None:
        delivery = self.gateway.get_delivery(delivery_id)
        if delivery.courier_id != current_user.id and not current_user.is_admin:
            raise AccessDeniedError(message)


class AdminDeliveryReadService:
    def __init__(self, repository, gateway):
        self.repository, self.gateway = repository, gateway

    @staticmethod
    def authorize(current_user):
        if not current_user.is_admin:
            raise AccessDeniedError("Доступно только администратору")

    def couriers(self, current_user, offset, limit):
        self.authorize(current_user)
        return self.repository.couriers(offset, limit)

    def deliveries(self, current_user, offset, limit):
        self.authorize(current_user)
        return [self.repository.delivery_view(row, current_user)
                for row in self.gateway.list_deliveries(offset, limit)]
