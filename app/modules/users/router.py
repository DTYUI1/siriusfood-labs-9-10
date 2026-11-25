"""
Транспортный слой модуля users (круг 4: Frameworks & Drivers).
Принимает HTTP-запрос, выполняет техническую валидацию, вызывает сервис
и переводит доменные исключения в HTTP-статусы.
"""
from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_current_user, get_user_repository, require_role
from app.core.identity import CurrentUser, RoleName
from app.core.rate_limit import rate_limit
from app.core.security import MAX_PASSWORD_BYTES, MIN_PASSWORD_LENGTH
from app.exceptions import DomainError
from app.http_errors import to_http_exception
from app.modules.users.repository import UserRepository
from app.modules.users.service import UserService
from app.schemas import RoleUpdate, UserCreate, UserProfile
from app.modules.admin_accounts.router import account_call, get_account_service
from app.modules.admin_accounts.service import AccountService

router = APIRouter()

MIN_PHONE_LENGTH = 10


def get_user_service(repository: UserRepository = Depends(get_user_repository)) -> UserService:
    """
    Сборка архитектурных слоёв: реализация репозитория передаётся в сервис
    через конструктор. Чтобы подменить хранилище (например, на
    InMemoryUserRepository), достаточно переопределить get_user_repository
    через app.dependency_overrides — service.py и router.py не меняются.
    """
    return UserService(repository)


# Защита от массовой регистрации: 5 аккаунтов в минуту с одного IP.
@router.post("/register", dependencies=[Depends(rate_limit(max_requests=5, window=60, scope="register"))])
def register_user(payload: UserCreate, service: UserService = Depends(get_user_service)):
    # Формат email проверяет Pydantic (EmailStr), формат телефона и длину пароля — здесь:
    # это техническая валидация входных данных, а не бизнес-правило.
    if payload.phone and len(payload.phone) < MIN_PHONE_LENGTH:
        raise HTTPException(status_code=400, detail="Некорректный формат номера телефона")

    if len(payload.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов",
        )

    if len(payload.password.encode()) > MAX_PASSWORD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Пароль должен занимать не больше {MAX_PASSWORD_BYTES} байт",
        )

    try:
        user = service.register_user(email=payload.email, password=payload.password, phone=payload.phone)
    except DomainError as error:
        raise to_http_exception(error) from error

    return {"status": "success", "user_id": user.id, "email": user.email}


@router.get("/me", response_model=UserProfile)
def get_me(
    current_user: CurrentUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
):
    """Профиль текущего пользователя. id берётся из токена — чужой профиль так не получить."""
    try:
        return service.get_profile(current_user.id)
    except DomainError as error:
        raise to_http_exception(error) from error


@router.post("/{user_id}/topup")
def top_up_balance(
    user_id: int,
    amount: float,
    current_user: CurrentUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
):
    # Техническая проверка положительности суммы пополнения.
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Сумма пополнения должна быть положительной")

    try:
        new_balance = service.top_up_balance(user_id=user_id, amount=amount, current_user=current_user)
    except DomainError as error:
        raise to_http_exception(error) from error

    return {"status": "success", "new_balance": new_balance}


@router.patch("/{user_id}/role", response_model=UserProfile)
def change_role(
    user_id: int,
    payload: RoleUpdate,
    _admin: CurrentUser = Depends(require_role(RoleName.ADMIN)),
    service: AccountService = Depends(get_account_service),
):
    """Назначение роли. Только для администратора."""
    return account_call(lambda: service.update(_admin, user_id, {"role": payload.role}))
