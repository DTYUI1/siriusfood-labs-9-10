"""ЛР7: платёж, fan-out, идемпотентность, Saga, outbox и повторы сообщений."""
import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from app.models import Delivery, KitchenOrder, Order, User
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService
from app.core.identity import CurrentUser, RoleName
from app.workers.kitchen_consumer import handle_event as kitchen
from app.workers.compensation_consumer import handle_event as compensate
from delivery_service.worker import handle_event as delivery
from event_bus.contracts import InvalidEvent, decode_event, new_event
from event_bus.models import Outbox, ProcessedEvent
from event_bus.rabbitmq import EventBus
from event_bus.storage import publish_batch


@pytest.fixture
def paid_order(session_factory, create_user, create_menu_item):
    student = create_user("events@sirius.ru", balance=1000)
    item = create_menu_item(price=150)
    with session_factory() as db:
        service = OrderService(OrderRepository(db))
        order_id = service.create_order(student.id, item, 2)
        service.pay_order(order_id, CurrentUser(student.id, RoleName.STUDENT))
        payload = json.loads(db.query(Outbox).one().payload)
    return student, order_id, payload


def test_pay_http_is_owner_only_idempotent_and_does_not_charge_twice(
    client, session_factory, create_user, create_menu_item, auth_headers,
):
    owner = create_user("payer@sirius.ru", balance=1000)
    stranger = create_user("stranger@sirius.ru")
    item_id = create_menu_item(price=150)
    response = client.post("/orders/create", json={"item_id": item_id, "quantity": 2}, headers=auth_headers(owner))
    assert response.status_code == 200
    order_id = response.json()["order_id"]
    url = f"/orders/{order_id}/pay"
    assert client.post(url).status_code == 401
    assert client.post(url, headers=auth_headers(stranger)).status_code == 403
    assert client.post(url, headers=auth_headers(owner)).status_code == 200
    assert client.post(url, headers=auth_headers(owner)).status_code == 200
    assert client.post("/orders/999999/pay", headers=auth_headers(owner)).status_code == 404
    with session_factory() as db:
        assert db.get(User, owner.id).balance == 700
        assert db.get(Order, order_id).status == "paid"
        assert db.query(Outbox).count() == 1


def test_cancelled_order_cannot_be_paid(client, session_factory, create_user, create_menu_item, auth_headers):
    owner = create_user("cancelpayer@sirius.ru", balance=1000)
    response = client.post("/orders/create", json={"item_id": create_menu_item(), "quantity": 1}, headers=auth_headers(owner))
    order_id = response.json()["order_id"]
    assert client.post(f"/orders/{order_id}/cancel", headers=auth_headers(owner)).status_code == 200
    assert client.post(f"/orders/{order_id}/pay", headers=auth_headers(owner)).status_code == 400
    with session_factory() as db:
        assert db.get(User, owner.id).balance == 1000
        assert db.query(Outbox).count() == 0


def test_payment_and_outbox_rollback_together(session_factory, create_user, create_menu_item, monkeypatch):
    owner = create_user("atomic@sirius.ru", balance=1000)
    item_id = create_menu_item()
    with session_factory() as db:
        service = OrderService(OrderRepository(db))
        order_id = service.create_order(owner.id, item_id, 1)
        def fail(*args, **kwargs):
            raise RuntimeError("outbox unavailable")
        monkeypatch.setattr("app.modules.orders.repository.append_event", fail)
        with pytest.raises(RuntimeError):
            service.pay_order(order_id, CurrentUser(owner.id, RoleName.STUDENT))
    with session_factory() as db:
        assert db.get(Order, order_id).status == "processing"
        assert db.query(Outbox).count() == 0


@pytest.mark.parametrize("reverse", [False, True])
def test_independent_consumers_and_duplicates(paid_order, session_factory, create_user, reverse):
    _, order_id, payload = paid_order
    courier = create_user("eventcourier@sirius.ru", role=RoleName.COURIER)
    handlers = [kitchen, delivery]
    if reverse:
        handlers.reverse()
    for handler in handlers:
        handler(payload, session_factory)
        handler(payload, session_factory)
        handler({**payload, "event_id": str(uuid4())}, session_factory)
    with session_factory() as db:
        assert db.query(KitchenOrder).filter_by(order_id=order_id).count() == 1
        assigned = db.query(Delivery).filter_by(order_id=order_id).one()
        assert assigned.courier_id == courier.id
        assert assigned.status == "in_progress"
        assert db.query(ProcessedEvent).count() == 4
        assert db.get(User, paid_order[0].id).balance == 700


def test_saga_refunds_once_and_cleans_both_consumers(paid_order, session_factory):
    owner, order_id, payload = paid_order
    kitchen(payload, session_factory)
    delivery(payload, session_factory)
    delivery(payload, session_factory)
    with session_factory() as db:
        failure = json.loads(db.query(Outbox).filter_by(routing_key="delivery.failed").one().payload)
    compensate(failure, session_factory)
    compensate(failure, session_factory)
    compensate({**failure, "event_id": str(uuid4())}, session_factory)
    with session_factory() as db:
        cancelled = json.loads(db.query(Outbox).filter_by(routing_key="order.cancelled").one().payload)
    for handler in (kitchen, delivery):
        handler(cancelled, session_factory)
        handler(cancelled, session_factory)
        # OrderPaid, задержавшийся за отменой, не воскрешает заказ.
        handler({**payload, "event_id": str(uuid4())}, session_factory)
    with session_factory() as db:
        assert db.get(Order, order_id).status == "cancelled"
        assert db.get(User, owner.id).balance == 1000
        assert db.query(KitchenOrder).one().status == "cancelled"
        assert db.query(Delivery).one().status == "cancelled"
        assert db.query(Outbox).filter_by(routing_key="order.cancelled").count() == 1


def test_cancel_before_kitchen_paid_does_not_create_kitchen_order(paid_order, session_factory):
    _, _, payload = paid_order
    delivery(payload, session_factory)
    with session_factory() as db:
        failure = json.loads(db.query(Outbox).filter_by(routing_key="delivery.failed").one().payload)
    compensate(failure, session_factory)
    kitchen(payload, session_factory)
    with session_factory() as db:
        assert db.query(KitchenOrder).count() == 0


def test_bad_event_cannot_write_partial_data(paid_order, session_factory):
    _, _, payload = paid_order
    for handler in (kitchen, delivery, compensate):
        with pytest.raises(InvalidEvent):
            handler({**payload, "customer_id": 9999}, session_factory)
    with session_factory() as db:
        assert db.query(ProcessedEvent).count() == 0
        assert db.query(KitchenOrder).count() == 0
        assert db.query(Delivery).count() == 0


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"{}", b"null", b"\xff"])
def test_invalid_payload(body):
    with pytest.raises(InvalidEvent):
        decode_event(body)


def test_outbox_retries_after_broker_failure(paid_order, session_factory):
    bus = AsyncMock()
    bus.publish_event.side_effect = ConnectionError("offline")
    with pytest.raises(ConnectionError):
        asyncio.run(publish_batch(session_factory, bus))
    with session_factory() as db:
        row = db.query(Outbox).one()
        assert row.published_at is None
        event_id = row.id
    bus.publish_event.side_effect = None
    assert asyncio.run(publish_batch(session_factory, bus)) == 1
    assert bus.publish_event.call_args.args[1]["event_id"] == event_id
    assert asyncio.run(publish_batch(session_factory, bus)) == 0
    with session_factory() as db:
        assert db.query(Outbox).one().published_at is not None


def test_outbox_commit_failure_allows_safe_duplicate(paid_order, session_factory, monkeypatch):
    """Сообщение подтверждено, commit падает: тот же event_id отправится снова."""
    from sqlalchemy import event
    bus = AsyncMock()
    def fail_commit(db):
        raise RuntimeError("commit lost")
    event.listen(session_factory.class_, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(publish_batch(session_factory, bus))
    finally:
        event.remove(session_factory.class_, "before_commit", fail_commit)
    assert asyncio.run(publish_batch(session_factory, bus)) == 1
    assert bus.publish_event.call_args_list[0] == bus.publish_event.call_args_list[1]


class Message:
    def __init__(self, body, headers=None):
        self.body = body
        self.headers = headers or {}
        self.content_type = "application/json"
        self.message_id = None
        self.ack = AsyncMock()
        self.reject = AsyncMock()
        self.nack = AsyncMock()


def test_poison_message_rejected_without_requeue():
    message = Message(b"not-json")
    asyncio.run(EventBus("unused").dispatch("kitchen", lambda _: None, message))
    message.reject.assert_awaited_once_with(requeue=False)
    message.ack.assert_not_awaited()


def test_transient_error_retries_then_deadletters():
    bus = EventBus("unused")
    bus.retry = AsyncMock()
    body = json.dumps(new_event("order.paid", 1, 1, 150)).encode()
    def unavailable(_):
        raise ConnectionError("database down")
    first = Message(body)
    asyncio.run(bus.dispatch("kitchen", unavailable, first))
    first.ack.assert_awaited_once()
    assert bus.retry.publish.call_args.args[0].headers["x-attempt"] == 2
    last = Message(body, {"x-attempt": 3})
    asyncio.run(bus.dispatch("kitchen", unavailable, last))
    last.reject.assert_awaited_once_with(requeue=False)
    assert bus.retry.publish.await_count == 1


def test_retry_copy_failure_keeps_original():
    bus = EventBus("unused")
    bus.retry = AsyncMock()
    bus.retry.publish.side_effect = ConnectionError("broker down")
    message = Message(json.dumps(new_event("order.paid", 1, 1, 150)).encode())
    def unavailable(_):
        raise ConnectionError("database down")
    asyncio.run(bus.dispatch("kitchen", unavailable, message))
    message.nack.assert_awaited_once_with(requeue=True)
    message.ack.assert_not_awaited()
