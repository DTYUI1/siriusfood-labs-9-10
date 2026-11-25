"""
gRPC-клиент микросервиса доставки (круг 3: адаптер порта DeliveryGateway).

Сервис монолита не знает о сети: этот класс переводит вызовы в gRPC,
а статусы gRPC — в доменные исключения, которые роутер превращает в HTTP-коды:

    NOT_FOUND            → NotFoundError            → 404
    FAILED_PRECONDITION  → BusinessRuleError        → 400
    PERMISSION_DENIED    → OperationForbiddenError  → 403
    DEADLINE_EXCEEDED    → ServiceTimeoutError      → 504
    UNAVAILABLE и прочее → ServiceUnavailableError  → 503
"""
import json
import logging
import threading
from collections.abc import Iterator
from datetime import timezone

import grpc

import delivery_pb2
import delivery_pb2_grpc
from app.core.config import settings
from app.exceptions import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    OperationForbiddenError,
    ServiceTimeoutError,
    ServiceUnavailableError,
)
from app.modules.delivery.service import ChatLine, DeliveryGateway, DeliveryInfo, DeliveryStatusUpdate

logger = logging.getLogger(__name__)

# Автоповтор при UNAVAILABLE — только для чтений. Запись повторять нельзя:
# если ответ потерялся после того, как сервис выполнил AssignCourier,
# повтор попытался бы назначить курьера второй раз.
_SERVICE_CONFIG = json.dumps(
    {
        "methodConfig": [
            {
                "name": [
                    {"service": "delivery.DeliveryService", "method": "GetDelivery"},
                    {"service": "delivery.DeliveryService", "method": "ListCourierDeliveries"},
                ],
                "retryPolicy": {
                    "maxAttempts": 3,
                    "initialBackoff": "0.1s",
                    "maxBackoff": "1s",
                    "backoffMultiplier": 2,
                    "retryableStatusCodes": ["UNAVAILABLE"],
                },
            }
        ]
    }
)

_DOMAIN_ERRORS = {
    grpc.StatusCode.NOT_FOUND: NotFoundError,
    grpc.StatusCode.FAILED_PRECONDITION: BusinessRuleError,
    grpc.StatusCode.ABORTED: ConflictError,
    grpc.StatusCode.PERMISSION_DENIED: OperationForbiddenError,
}


def _translate(error: grpc.RpcError) -> Exception:
    """Статус gRPC → доменное исключение. Детали технических сбоев клиенту не показываются."""
    code = error.code()
    if code in _DOMAIN_ERRORS:
        return _DOMAIN_ERRORS[code](error.details())
    if code == grpc.StatusCode.DEADLINE_EXCEEDED:
        logger.warning("Сервис доставки не ответил вовремя: %s", error.details())
        return ServiceTimeoutError("Сервис доставки не ответил вовремя")
    # UNAVAILABLE — сервис не запущен или сеть недоступна; UNAUTHENTICATED — не тот токен
    # в настройках; RESOURCE_EXHAUSTED — заняты все потоки сервиса.
    logger.error("Сервис доставки: %s %s", code.name, error.details())
    return ServiceUnavailableError("Сервис доставки недоступен")


def _to_info(message: delivery_pb2.Delivery) -> DeliveryInfo:
    return DeliveryInfo(
        id=message.id,
        order_id=message.order_id,
        courier_id=message.courier_id or None,
        customer_id=message.customer_id or None,
        status=message.status,
    )


class DeliveryGrpcClient(DeliveryGateway):
    """
    Канал создаётся один раз на процесс: это HTTP/2-соединение, по которому
    идут все вызовы. Новый канал на каждый запрос — лишнее TCP- и HTTP/2-рукопожатие.
    """

    def __init__(self, target: str, token: str, timeout: float):
        # insecure_channel — без TLS: сервис слушает только localhost/внутреннюю сеть.
        self.channel = grpc.insecure_channel(
            target,
            options=[
                ("grpc.enable_retries", 1),
                ("grpc.service_config", _SERVICE_CONFIG),
                # Пока сервис лежит, канал переподключается с растущей паузой — по умолчанию
                # до 120 с, и после долгой аварии монолит отвечал бы 503 уже поднятому сервису.
                # С пределом 2 с после рестарта сервиса монолит подключается за ~1 с.
                ("grpc.initial_reconnect_backoff_ms", 200),
                ("grpc.max_reconnect_backoff_ms", 2000),
            ],
        )
        self.stub = delivery_pb2_grpc.DeliveryServiceStub(self.channel)
        self.timeout = timeout
        self._auth = ("authorization", f"Bearer {token}")

    def assign_courier(self, order_id: int, courier_id: int) -> int:
        request = delivery_pb2.AssignCourierRequest(order_id=order_id, courier_id=courier_id)
        return self._call(self.stub.AssignCourier, request).delivery_id

    def get_delivery(self, delivery_id: int) -> DeliveryInfo:
        response = self._call(self.stub.GetDelivery, delivery_pb2.GetDeliveryRequest(delivery_id=delivery_id))
        return _to_info(response.delivery)

    def courier_deliveries(self, courier_id: int) -> list[DeliveryInfo]:
        request = delivery_pb2.ListCourierDeliveriesRequest(courier_id=courier_id)
        return [_to_info(delivery) for delivery in self._call(self.stub.ListCourierDeliveries, request).deliveries]

    def list_deliveries(self, offset: int, limit: int) -> list[DeliveryInfo]:
        request = delivery_pb2.ListDeliveriesRequest(offset=offset, limit=limit)
        return [_to_info(row) for row in self._call(self.stub.ListDeliveries, request).deliveries]

    def pick_up(self, delivery_id: int) -> None:
        self._call(self.stub.PickUpOrder, delivery_pb2.PickUpOrderRequest(delivery_id=delivery_id))

    def complete(self, delivery_id: int) -> None:
        self._call(self.stub.CompleteDelivery, delivery_pb2.CompleteDeliveryRequest(delivery_id=delivery_id))

    def track(self, delivery_id: int) -> Iterator[DeliveryStatusUpdate]:
        # Без дедлайна: поток живёт, пока доставка не завершится. Сервер сам
        # закрывает его после конечного статуса или через TRACK_MAX_SECONDS.
        stream = self.stub.TrackDelivery(
            delivery_pb2.TrackDeliveryRequest(delivery_id=delivery_id), metadata=[self._auth]
        )
        for update in self._stream(stream):
            yield DeliveryStatusUpdate(
                delivery_id=update.delivery_id,
                status=update.status,
                observed_at=update.observed_at.ToDatetime(tzinfo=timezone.utc),
                final=update.final,
            )

    def chat(self, delivery_id: int, user_id: int, outgoing: Iterator[str]) -> Iterator[ChatLine]:
        requests = (delivery_pb2.ChatMessage(text=text) for text in outgoing)
        stream = self.stub.ChatWithCourier(
            requests,
            metadata=[self._auth, ("x-delivery-id", str(delivery_id)), ("x-user-id", str(user_id))],
        )
        for message in self._stream(stream):
            yield ChatLine(
                sender_id=message.sender_id,
                sender_role=message.sender_role,
                text=message.text,
                sent_at=message.sent_at.ToDatetime(tzinfo=timezone.utc),
            )

    def close(self) -> None:
        self.channel.close()

    def _call(self, method, request):
        try:
            return method(request, timeout=self.timeout, metadata=[self._auth])
        except grpc.RpcError as error:
            raise _translate(error) from error

    @staticmethod
    def _stream(stream):
        try:
            yield from stream
        except grpc.RpcError as error:
            raise _translate(error) from error
        finally:
            # Клиент перестал читать (Ctrl+C, исключение) — поток на сервере отменяется.
            stream.cancel()


_client: DeliveryGrpcClient | None = None
_client_lock = threading.Lock()


def get_delivery_gateway() -> DeliveryGateway:
    """Общий клиент на процесс. Подменяется через app.dependency_overrides."""
    global _client
    with _client_lock:
        if _client is None:
            _client = DeliveryGrpcClient(
                settings.DELIVERY_GRPC_TARGET, settings.DELIVERY_GRPC_TOKEN, settings.DELIVERY_GRPC_TIMEOUT
            )
        return _client


def close_delivery_gateway() -> None:
    """Закрытие канала при остановке приложения."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None
