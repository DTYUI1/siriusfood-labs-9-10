"""Bounded administrator-only DTOs and validation without secret echoes."""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy.exc import IntegrityError

from app.core.dependencies import get_user_repository, require_role
from app.core.identity import CurrentUser, RoleName
from app.core.security import MAX_PASSWORD_BYTES, MIN_PASSWORD_LENGTH
from app.exceptions import DomainError
from app.http_errors import to_http_exception
from app.modules.admin_accounts.service import AccountConflict, AccountService
from app.modules.users.repository import UserRepository
from app.modules.delivery.grpc_client import get_delivery_gateway


class SafeAccountRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def safe(request):
            try:
                return await original(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={"detail": {"code": "invalid_account_input", "message": "Проверьте поля аккаунта"}})
            except HTTPException as error:
                if isinstance(error.detail, dict):
                    raise
                code = {401: "not_authenticated", 403: "forbidden", 404: "not_found"}.get(error.status_code, "request_failed")
                return JSONResponse(status_code=error.status_code, headers=error.headers,
                                    content={"detail": {"code": code, "message": str(error.detail)}})
        return safe


router = APIRouter(route_class=SafeAccountRoute)


class AccountInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("phone", mode="before", check_fields=False)
    @classmethod
    def trim_phone(cls, value):
        return value.strip() if isinstance(value, str) else value


class AccountCreate(AccountInput):
    email: EmailStr
    password: str = Field(repr=False)
    phone: str | None = Field(default=None, min_length=10, max_length=32)
    role: RoleName = RoleName.STUDENT
    is_blocked: bool = False


class AccountUpdate(AccountInput):
    email: EmailStr | None = None
    phone: str | None = Field(default=None, min_length=10, max_length=32)
    role: RoleName | None = None
    is_blocked: bool | None = None


class PasswordReset(AccountInput):
    # Password whitespace is meaningful; never strip or include it in repr/errors.
    model_config = ConfigDict(extra="forbid")
    new_password: str = Field(repr=False)


def get_account_service(users: UserRepository = Depends(get_user_repository), gateway=Depends(get_delivery_gateway)):
    return AccountService(users, gateway.courier_deliveries)


def account_view(user):
    return {key: getattr(user, key) for key in ("id", "email", "phone", "role", "is_blocked", "balance")}


def account_call(callback):
    try:
        return callback()
    except AccountConflict as error:
        raise HTTPException(409, detail={"code": error.code, "message": error.message}) from error
    except IntegrityError as error:
        raise HTTPException(409, detail={"code": "duplicate_email", "message": "Email уже используется"}) from error
    except DomainError as error:
        http = to_http_exception(error)
        http.detail = {"code": "account_unavailable", "message": str(error)}
        raise http from error


def validate_password(password):
    if len(password) < MIN_PASSWORD_LENGTH or len(password.encode()) > MAX_PASSWORD_BYTES:
        raise HTTPException(422, detail={"code": "invalid_password", "message": "Пароль: минимум 8 символов, максимум 72 байта"})


@router.get("/users")
def list_accounts(offset: int = Query(0, ge=0, le=2147483647), limit: int = Query(25, ge=1, le=100),
                  q: str | None = Query(None, max_length=200), role: RoleName | None = None,
                  is_blocked: bool | None = None, actor: CurrentUser = Depends(require_role(RoleName.ADMIN)),
                  service: AccountService = Depends(get_account_service)):
    items, total = account_call(lambda: service.list(actor, offset=offset, limit=limit, q=q, role=role, is_blocked=is_blocked))
    return {"items": [account_view(u) for u in items], "total": total, "offset": offset, "limit": limit}


@router.get("/users/{user_id}")
def get_account(user_id: int, actor: CurrentUser = Depends(require_role(RoleName.ADMIN)), service: AccountService = Depends(get_account_service)):
    return account_view(account_call(lambda: service.get(actor, user_id)))


@router.post("/users", status_code=201)
def create_account(payload: AccountCreate, actor: CurrentUser = Depends(require_role(RoleName.ADMIN)), service: AccountService = Depends(get_account_service)):
    validate_password(payload.password)
    return account_view(account_call(lambda: service.create(actor, **payload.model_dump())))


@router.patch("/users/{user_id}")
def update_account(user_id: int, payload: AccountUpdate, actor: CurrentUser = Depends(require_role(RoleName.ADMIN)), service: AccountService = Depends(get_account_service)):
    changes = payload.model_dump(exclude_unset=True)
    if not changes or any(v is None for k, v in changes.items() if k != "phone"):
        raise HTTPException(422, detail={"code": "invalid_account_input", "message": "Укажите изменяемые поля"})
    return account_view(account_call(lambda: service.update(actor, user_id, changes)))


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, payload: PasswordReset, actor: CurrentUser = Depends(require_role(RoleName.ADMIN)), service: AccountService = Depends(get_account_service)):
    validate_password(payload.new_password)
    account_call(lambda: service.reset_password(actor, user_id, payload.new_password))
    return {"status": "success", "user_id": user_id, "sessions_revoked": True}
