"""Версионированный контракт событий (только стандартная библиотека)."""
import json
import math
from datetime import datetime, timezone
from uuid import UUID, uuid4

from event_bus.observability import current_correlation_id, valid_correlation_id

EVENT_TYPES = {
    "order.paid": "OrderPaid",
    "delivery.failed": "DeliveryFailed",
    "order.cancelled": "OrderCancelled",
}


class InvalidEvent(ValueError):
    """Сообщение нельзя обработать даже после повторной доставки."""


def new_event(routing_key, order_id, customer_id, amount, **extra):
    event_id = str(uuid4())
    return {
        "event_id": event_id,
        "correlation_id": current_correlation_id() or event_id,
        "event_type": EVENT_TYPES[routing_key],
        "schema_version": 1,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "order_id": order_id,
        "customer_id": customer_id,
        "amount": float(amount),
        **extra,
    }


def validate_event(payload):
    if not isinstance(payload, dict):
        raise InvalidEvent("JSON должен быть объектом")
    try:
        UUID(payload["event_id"])
        # Поле опционально для обратной совместимости с очередью и outbox.
        if "correlation_id" in payload and not valid_correlation_id(payload["correlation_id"]):
            raise ValueError("Неверный correlation_id")
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise ValueError("Версия схемы")
        if payload["event_type"] not in EVENT_TYPES.values():
            raise ValueError("Тип события")
        for name in ("order_id", "customer_id"):
            if type(payload[name]) is not int or payload[name] <= 0:
                raise ValueError(name)
        amount = payload["amount"]
        if type(amount) not in (float, int) or not math.isfinite(amount) or amount <= 0:
            raise ValueError("Сумма")
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError) as error:
        raise InvalidEvent("Неверный контракт события") from error
    return payload


def decode_event(body):
    try:
        return validate_event(json.loads(body))
    except (ValueError, UnicodeError) as error:
        raise InvalidEvent("Невалидное событие") from error
