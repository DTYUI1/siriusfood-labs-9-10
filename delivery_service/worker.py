"""Отдельный consumer доставки. Не импортирует код монолита."""
import asyncio
import logging
from sqlalchemy import exists, select
from delivery_service.config import settings
from delivery_service.domain import ACTIVE_STATUSES
from delivery_service.models import Delivery, Order, Role, User
from event_bus.contracts import InvalidEvent, validate_event
from event_bus.models import ProcessedEvent
from event_bus.observability import event_log_fields
from event_bus.rabbitmq import consume_forever
from event_bus.storage import append_event, make_session_factory


def handle_event(payload, session_factory):
    validate_event(payload)
    with session_factory() as db, db.begin():
        order = db.get(Order, payload["order_id"], with_for_update=True)
        if order is None or order.user_id != payload["customer_id"] or order.total_cost != payload["amount"]:
            raise InvalidEvent("Заказ не соответствует событию")
        key = ("delivery", payload["event_id"])
        if db.get(ProcessedEvent, key):
            return
        delivery = db.query(Delivery).filter_by(order_id=order.id).first()
        if payload["event_type"] == "OrderPaid":
            if order.status == "paid" and delivery is None:
                busy = exists().where(Delivery.courier_id == User.id, Delivery.status.in_(ACTIVE_STATUSES))
                courier = db.scalars(
                    select(User).join(Role).where(Role.name == "courier", User.is_blocked.is_(False), ~busy)
                    .order_by(User.id).with_for_update(of=User, skip_locked=True).limit(1)
                ).first()
                if courier:
                    db.add(Delivery(order_id=order.id, courier_id=courier.id, status="in_progress"))
                else:
                    db.add(Delivery(order_id=order.id, courier_id=None, status="failed"))
                    append_event(db, "delivery.failed", order.id, order.user_id, order.total_cost,
                                 reason="no_available_courier", causation_id=payload["event_id"])
        elif payload["event_type"] == "OrderCancelled":
            if order.status != "cancelled":
                raise InvalidEvent("Заказ ещё не отменён")
            if delivery and delivery.status != "completed":
                delivery.status = "cancelled"
        else:
            raise InvalidEvent("Неподдерживаемое событие")
        db.add(ProcessedEvent(consumer="delivery", event_id=payload["event_id"]))
    logging.info("delivery_event_processed", extra=event_log_fields(payload))


def main():
    factory = make_session_factory(settings.DATABASE_URL)
    asyncio.run(consume_forever(settings.RABBITMQ_URL, "delivery", lambda payload: handle_event(payload, factory)))
