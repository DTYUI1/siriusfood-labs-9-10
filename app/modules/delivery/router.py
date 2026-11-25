"""
Транспортный слой модуля delivery (круг 4).
Роль проверяется здесь (require_role), права на конкретную доставку — в сервисе,
правила самой доставки — в микросервисе delivery_service (вызов по gRPC).
Пути и тела ответов не изменились после выноса модуля из монолита.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user, require_role
from app.core.identity import CurrentUser, RoleName
from app.exceptions import DomainError
from app.http_errors import to_http_exception
from app.modules.delivery.grpc_client import get_delivery_gateway
from app.modules.delivery.service import DeliveryGateway, DeliveryInfo, DeliveryService
from app.schemas import DeliveryAssign
from app.database import get_db
from app.modules.delivery.read_repository import DeliveryReadRepository

router = APIRouter()


def get_delivery_service(gateway: DeliveryGateway = Depends(get_delivery_gateway), db: Session = Depends(get_db)) -> DeliveryService:
    """Сборка слоёв: вместо репозитория в сервис передаётся gRPC-клиент микросервиса."""
    return DeliveryService(gateway, DeliveryReadRepository(db))


def _to_dict(delivery: DeliveryInfo) -> dict:
    return {
        "delivery_id": delivery.id,
        "order_id": delivery.order_id,
        "courier_id": delivery.courier_id,
        "status": delivery.status,
    }


@router.post("/assign")
def assign_courier(
    payload: DeliveryAssign,
    # Курьеров назначает кухня или администратор, но не сами курьеры и не студенты.
    _dispatcher: CurrentUser = Depends(require_role(RoleName.COOK, RoleName.ADMIN)),
    service: DeliveryService = Depends(get_delivery_service),
):
    try:
        delivery_id = service.assign_courier(order_id=payload.order_id, courier_id=payload.courier_id)
    except DomainError as error:
        raise to_http_exception(error) from error

    return {"status": "success", "delivery_id": delivery_id}


@router.get("/my")
def my_deliveries(
    current_user: CurrentUser = Depends(require_role(RoleName.COURIER)),
    service: DeliveryService = Depends(get_delivery_service),
):
    """Доставки текущего курьера. Чужие доставки в список не попадают."""
    try:
        deliveries = service.get_courier_deliveries(current_user)
    except DomainError as error:
        raise to_http_exception(error) from error

    return [service.view(delivery, current_user) for delivery in deliveries]


@router.get("/{delivery_id}")
def get_delivery(
    delivery_id: int,
    current_user: CurrentUser = Depends(get_current_user),
    service: DeliveryService = Depends(get_delivery_service),
):
    try:
        delivery = service.get_delivery(delivery_id, current_user)
    except DomainError as error:
        raise to_http_exception(error) from error

    return service.view(delivery, current_user)


@router.patch("/{delivery_id}/pickup")
def pick_up_order(
    delivery_id: int,
    current_user: CurrentUser = Depends(require_role(RoleName.COURIER, RoleName.ADMIN)),
    service: DeliveryService = Depends(get_delivery_service),
):
    """Курьер забрал заказ: статус доставки in_progress → on_the_way."""
    try:
        service.pick_up(delivery_id, current_user)
    except DomainError as error:
        raise to_http_exception(error) from error

    return {"status": "success"}


@router.patch("/{delivery_id}/complete")
def complete_delivery(
    delivery_id: int,
    current_user: CurrentUser = Depends(require_role(RoleName.COURIER, RoleName.ADMIN)),
    service: DeliveryService = Depends(get_delivery_service),
):
    try:
        service.complete_delivery(delivery_id, current_user)
    except DomainError as error:
        raise to_http_exception(error) from error

    return {"status": "success"}
