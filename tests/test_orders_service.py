"""
Unit-тесты бизнес-логики модуля orders.
Вместо OrderRepository передаётся заглушка: СУБД не требуется.
"""
from types import SimpleNamespace

import pytest

from app.core.identity import CurrentUser, RoleName
from app.exceptions import AccessDeniedError, BusinessRuleError, NotFoundError
from app.modules.orders.service import OrderCreated, OrderService

OWNER = CurrentUser(id=1, role=RoleName.STUDENT)
OTHER_STUDENT = CurrentUser(id=2, role=RoleName.STUDENT)
ADMIN = CurrentUser(id=3, role=RoleName.ADMIN)


class FakeOrderRepository:
    def __init__(self, users=(), items=(), orders=()):
        self.users = {user.id: user for user in users}
        self.items = {item.id: item for item in items}
        self.orders = {order.id: order for order in orders}
        self.saved_orders = []
        self.cancelled = []

    def get_user_by_id(self, user_id):
        return self.users.get(user_id)

    def get_menu_item_by_id(self, item_id):
        return self.items.get(item_id)

    def get_order(self, order_id):
        return self.orders.get(order_id)

    def get_orders_by_status(self, status):
        return [order for order in self.orders.values() if order.status == status]

    def save_order(self, user_id, item_id, quantity, new_balance, total_cost):
        self.saved_orders.append((user_id, item_id, quantity, new_balance, total_cost))
        return len(self.saved_orders)

    def cancel_order(self, order_id, expected_status, user_id, refund):
        order = self.orders[order_id]
        if order.status != expected_status:
            return False
        order.status = "cancelled"
        self.cancelled.append((order_id, user_id, refund))
        return True


@pytest.fixture
def repository():
    return FakeOrderRepository(
        users=[SimpleNamespace(id=1, balance=1000.0, email="student@sirius.ru", phone="+79990001122")],
        items=[
            SimpleNamespace(id=1, price=250.0, is_available=True),
            SimpleNamespace(id=2, price=50.0, is_available=False),
        ],
        orders=[
            SimpleNamespace(id=10, user_id=1, status="processing", total_cost=500.0),
            SimpleNamespace(id=11, user_id=1, status="delivered", total_cost=250.0),
        ],
    )


def test_create_order_charges_balance(repository):
    order_id = OrderService(repository).create_order(user_id=1, item_id=1, quantity=2)

    assert order_id == 1
    assert repository.saved_orders == [(1, 1, 2, 500.0, 500.0)]


@pytest.mark.parametrize(
    ("user_id", "item_id", "quantity", "error", "message"),
    [
        (999, 1, 1, NotFoundError, "Пользователь не найден"),
        (1, 999, 1, NotFoundError, "Блюдо не найдено"),
        (1, 2, 1, BusinessRuleError, "Блюдо недоступно"),
        (1, 1, 5, BusinessRuleError, "Недостаточно средств на балансе студента"),
    ],
    ids=["unknown-user", "unknown-item", "unavailable-item", "not-enough-money"],
)
def test_create_order_violations(repository, user_id, item_id, quantity, error, message):
    with pytest.raises(error, match=message):
        OrderService(repository).create_order(user_id=user_id, item_id=item_id, quantity=quantity)

    assert repository.saved_orders == []


def test_owner_cancels_order_with_refund(repository):
    OrderService(repository).cancel_order(10, OWNER)

    assert repository.cancelled == [(10, 1, 500.0)]


def test_cancel_foreign_order_is_denied(repository):
    """IDOR: студент не может отменить заказ другого студента, подставив его order_id."""
    with pytest.raises(AccessDeniedError):
        OrderService(repository).cancel_order(10, OTHER_STUDENT)

    assert repository.orders[10].status == "processing"
    assert repository.cancelled == []


def test_admin_can_cancel_any_order(repository):
    OrderService(repository).cancel_order(10, ADMIN)

    assert repository.cancelled == [(10, 1, 500.0)]


def test_cancel_unknown_order(repository):
    with pytest.raises(NotFoundError, match="Заказ не найден"):
        OrderService(repository).cancel_order(999, OWNER)


def test_cancel_delivered_order_is_rejected(repository):
    with pytest.raises(BusinessRuleError, match="ещё готовится"):
        OrderService(repository).cancel_order(11, OWNER)

    assert repository.cancelled == []


def test_second_cancel_does_not_refund_twice(repository):
    service = OrderService(repository)
    service.cancel_order(10, OWNER)

    with pytest.raises(BusinessRuleError):
        service.cancel_order(10, OWNER)
    assert len(repository.cancelled) == 1


def test_kitchen_sees_only_processing_orders(repository):
    assert [order.id for order in OrderService(repository).get_kitchen_orders()] == [10]


class FakeNotifier:
    def __init__(self):
        self.events = []

    def order_created(self, event):
        self.events.append(event)


def test_created_order_is_announced_after_saving(repository):
    notifier = FakeNotifier()

    order_id = OrderService(repository, notifier).create_order(user_id=1, item_id=1, quantity=2)

    assert notifier.events == [
        OrderCreated(order_id=order_id, user_id=1, email="student@sirius.ru", phone="+79990001122", total_cost=500.0)
    ]
    assert repository.saved_orders, "уведомление только после сохранения"


def test_rejected_order_is_not_announced(repository):
    notifier = FakeNotifier()

    with pytest.raises(BusinessRuleError):
        OrderService(repository, notifier).create_order(user_id=1, item_id=1, quantity=5)

    assert notifier.events == []
