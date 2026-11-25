"""
Транспортный слой модуля orders (круг 4: Frameworks & Drivers).
Принимает HTTP-запрос, собирает слои, вызывает сервис и переводит
доменные исключения в HTTP-статусы.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user, require_role
from app.core.identity import CurrentUser, RoleName
from app.database import get_db
from app.exceptions import DomainError
from app.http_errors import to_http_exception
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderNotifier, OrderService
from app.modules.orders.views import kitchen_status, recipient_view
from app.notifications.notifiers import get_order_notifier
from app.schemas import KitchenStatusUpdate, OrderCreate

logger = logging.getLogger(__name__)
router = APIRouter()
# Отдельный роутер, чтобы путь был /kitchen/orders, а не /orders/kitchen/orders.
kitchen_router = APIRouter()
admin_router = APIRouter()


def order_view(order, *, admin: bool = False):
    result = {"order_id": order.id, "item_id": order.item_id,
              "item_name": order.item.name if order.item else None,
              "quantity": order.quantity, "total_price": order.total_cost,
              "status": order.status, "created_at": order.created_at,
              "recipient": recipient_view(order), "kitchen_status": kitchen_status(order)}
    if admin:
        result["user_id"] = order.user_id
    return result


def get_order_service(
    db: Session = Depends(get_db),
    notifier: OrderNotifier = Depends(get_order_notifier),
) -> OrderService:
    """Сборка архитектурных слоёв модуля orders: репозиторий и уведомления передаются в сервис."""
    return OrderService(OrderRepository(db), notifier)


@router.get("/my")
def own_orders(
    current_user: CurrentUser = Depends(require_role(RoleName.STUDENT)),
    service: OrderService = Depends(get_order_service),
):
    return [order_view(order) for order in service.own_orders(current_user)]


@admin_router.get("/orders")
def admin_orders(
    offset: int = Query(0, ge=0, le=2147483647), limit: int = Query(100, ge=1, le=100),
    current_user: CurrentUser = Depends(require_role(RoleName.ADMIN)),
    service: OrderService = Depends(get_order_service),
):
    return [order_view(order, admin=True) for order in service.admin_orders(current_user, offset, limit)]


@router.post("/create")
def create_order(
    payload: OrderCreate,
    # Заказывают только студенты: повар и курьер не создают заказы (RBAC).
    current_user: CurrentUser = Depends(require_role(RoleName.STUDENT)),
    service: OrderService = Depends(get_order_service),
):
    # Техническая валидация уровня транспортного протокола.
    if payload.quantity <= 0:
        raise HTTPException(status_code=400, detail="Количество должно быть больше нуля")

    # Делегирование выполнения сценария слою бизнес-логики.
    try:
        order_id = service.create_order(
            user_id=current_user.id,
            item_id=payload.item_id,
            quantity=payload.quantity,
            recipient=payload.recipient.model_dump() if payload.recipient else None,
        )
    except DomainError as error:
        # Трансляция внутреннего исключения в HTTP-статус.
        raise to_http_exception(error) from error

    logger.info("order_created", extra={"order_id": order_id})
    return {"status": "success", "order_id": order_id}


@router.post("/{order_id}/pay")
def pay_order(
    order_id: int,
    current_user: CurrentUser = Depends(get_current_user),
    service: OrderService = Depends(get_order_service),
):
    try:
        service.pay_order(order_id, current_user)
    except DomainError as error:
        raise to_http_exception(error) from error
    return {"status": "success", "order_id": order_id}


@router.post("/{order_id}/cancel")
def cancel_order(
    order_id: int,
    current_user: CurrentUser = Depends(get_current_user),
    service: OrderService = Depends(get_order_service),
):
    # Чей это заказ, проверяет сервис: это бизнес-правило, а не HTTP-валидация.
    try:
        service.cancel_order(order_id, current_user)
    except DomainError as error:
        raise to_http_exception(error) from error

    return {"status": "success"}


@kitchen_router.get("/orders")
def kitchen_orders(
    _cook: CurrentUser = Depends(require_role(RoleName.COOK, RoleName.ADMIN)),
    service: OrderService = Depends(get_order_service),
):
    """Очередь кухни. Студент и курьер получают 403."""
    return [
        {
            "order_id": order.id,
            "item_id": order.item_id,
            "item_name": order.item.name if order.item else None,
            "quantity": order.quantity,
            "status": order.status,
            "kitchen_status": kitchen_status(order),
            "recipient_name": (recipient_view(order) or {}).get("name"),
        }
        for order in service.get_kitchen_orders()
    ]


@kitchen_router.patch("/orders/{order_id}/status")
def advance_kitchen(order_id: int, payload: KitchenStatusUpdate,
                    _cook: CurrentUser = Depends(require_role(RoleName.COOK, RoleName.ADMIN)),
                    service: OrderService = Depends(get_order_service)):
    try:
        return service.advance_kitchen(order_id, payload.status)
    except DomainError as error:
        raise to_http_exception(error) from error
