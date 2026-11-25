"""Approved admin-only read views; no dispatch or financial behaviour changes."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.core.dependencies import require_role
from app.core.identity import CurrentUser, RoleName
from app.database import get_db
from app.modules.delivery.admin_repository import AdminDeliveryRepository
from app.modules.delivery.service import AdminDeliveryReadService
from app.modules.delivery.grpc_client import get_delivery_gateway
from app.exceptions import DomainError
from app.http_errors import to_http_exception

router = APIRouter()


def read_service(db: Session = Depends(get_db), gateway=Depends(get_delivery_gateway)):
    return AdminDeliveryReadService(AdminDeliveryRepository(db), gateway)


@router.get("/couriers")
def couriers(
    offset: int = Query(0, ge=0, le=2147483647), limit: int = Query(100, ge=1, le=100),
    admin: CurrentUser = Depends(require_role(RoleName.ADMIN)), service=Depends(read_service),
):
    return service.couriers(admin, offset, limit)


@router.get("/deliveries")
def deliveries(
    offset: int = Query(0, ge=0, le=2147483647), limit: int = Query(100, ge=1, le=100),
    admin: CurrentUser = Depends(require_role(RoleName.ADMIN)), service=Depends(read_service),
):
    try:
        return service.deliveries(admin, offset, limit)
    except DomainError as error:
        raise to_http_exception(error) from error
