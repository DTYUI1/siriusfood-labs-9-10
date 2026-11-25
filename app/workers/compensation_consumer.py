"""Saga: DeliveryFailed -> отмена + возврат + OrderCancelled в одной транзакции."""
import asyncio
import logging
from sqlalchemy import update
from app.core.config import settings
from app.database import SessionLocal
from app.models import Order, User
from event_bus.contracts import InvalidEvent, validate_event
from event_bus.models import ProcessedEvent
from event_bus.observability import event_log_fields
from event_bus.rabbitmq import consume_forever
from event_bus.storage import append_event


def handle_event(payload, session_factory=SessionLocal):
    validate_event(payload)
    if payload["event_type"] != "DeliveryFailed":
        raise InvalidEvent("Ожидается DeliveryFailed")
    with session_factory() as db, db.begin():
        order = db.get(Order, payload["order_id"], with_for_update=True)
        if order is None or order.user_id != payload["customer_id"] or order.total_cost != payload["amount"]:
            raise InvalidEvent("Заказ не соответствует событию")
        key = ("compensation", payload["event_id"])
        if db.get(ProcessedEvent, key):
            return
        if order.status == "paid":
            order.status = "cancelled"
            db.execute(update(User).where(User.id == order.user_id).values(balance=User.balance + order.total_cost))
            append_event(db, "order.cancelled", order.id, order.user_id, order.total_cost,
                         reason="delivery_failed", causation_id=payload["event_id"])
        db.add(ProcessedEvent(consumer="compensation", event_id=payload["event_id"]))
    logging.info("compensation_event_processed", extra=event_log_fields(payload))


if __name__ == "__main__":
    asyncio.run(consume_forever(settings.RABBITMQ_URL, "compensation", handle_event))
