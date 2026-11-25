"""HTTP → JWT → сервис заказа → мигрированная PostgreSQL."""
import json

from sqlalchemy import select, text

from app.models import Order, User
from event_bus.models import Outbox


def test_create_order_persists_balance_and_line_item(client, create_user, create_menu_item,
                                                     auth_headers, session_factory):
    user = create_user("order@integration.invalid", balance=1000)
    item_id = create_menu_item(price=150)
    response = client.post("/orders/create", headers=auth_headers(user),
                           json={"item_id": item_id, "quantity": 2})
    assert response.status_code == 200, response.text

    with session_factory() as db:
        assert db.bind.dialect.name == "postgresql"
        assert db.scalar(text("SELECT current_database()")) == "sirius_integration"
        order = db.get(Order, response.json()["order_id"])
        assert (order.user_id, order.item_id, order.quantity, order.total_cost, order.status) == (
            user.id, item_id, 2, 300, "processing",
        )
        assert db.get(User, user.id).balance == 700


def test_repeat_payment_writes_one_outbox_event_with_original_correlation(
    client, create_user, create_menu_item, auth_headers, session_factory,
):
    user = create_user("pay@integration.invalid", balance=1000)
    headers = auth_headers(user)
    order_id = client.post("/orders/create", headers=headers,
                           json={"item_id": create_menu_item(price=150), "quantity": 1}).json()["order_id"]

    correlation = "sirius-integration-payment"
    first = client.post(f"/orders/{order_id}/pay", headers={**headers, "X-Correlation-ID": correlation})
    again = client.post(f"/orders/{order_id}/pay", headers={**headers, "X-Correlation-ID": "retry"})
    assert first.status_code == again.status_code == 200, (first.text, again.text)
    assert first.headers["X-Correlation-ID"] == correlation
    assert again.headers["X-Correlation-ID"] == "retry"

    with session_factory() as db:
        assert db.get(Order, order_id).status == "paid"
        assert db.get(User, user.id).balance == 850
        event, = db.scalars(select(Outbox)).all()
        assert event.routing_key == "order.paid"
        assert event.published_at is None
        assert json.loads(event.payload)["correlation_id"] == correlation
