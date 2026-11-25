"""Кухня независимо обрабатывает OrderPaid и OrderCancelled."""
import asyncio
import logging
from app.core.config import settings
from app.database import SessionLocal
from app.models import KitchenOrder, Order
from event_bus.contracts import InvalidEvent, validate_event
from event_bus.models import ProcessedEvent
from event_bus.observability import event_log_fields
from event_bus.rabbitmq import consume_forever


def handle_event(payload, session_factory=SessionLocal):
    validate_event(payload)
    with session_factory() as db, db.begin():
        order = db.get(Order, payload["order_id"], with_for_update=True)
        if order is None or order.user_id != payload["customer_id"] or order.total_cost != payload["amount"]:
            raise InvalidEvent("Заказ не соответствует событию")
        key = ("kitchen", payload["event_id"])
        if db.get(ProcessedEvent, key):
            return
        kitchen = db.query(KitchenOrder).filter_by(order_id=order.id).first()
        if payload["event_type"] == "OrderPaid":
            if order.status == "paid" and kitchen is None:
                db.add(KitchenOrder(order_id=order.id, status="pending"))
        elif payload["event_type"] == "OrderCancelled":
            if order.status != "cancelled":
                raise InvalidEvent("Заказ ещё не отменён")
            if kitchen:
                kitchen.status = "cancelled"
        else:
            raise InvalidEvent("Неподдерживаемое событие")
        db.add(ProcessedEvent(consumer="kitchen", event_id=payload["event_id"]))
    logging.info("kitchen_event_processed", extra=event_log_fields(payload))


if __name__ == "__main__":
    asyncio.run(consume_forever(settings.RABBITMQ_URL, "kitchen", handle_event))
