"""
Транспортный слой модуля auth (круг 4): вход и обновление токенов.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm

from app.core.dependencies import get_auth_service
from app.core.rate_limit import rate_limit
from app.core.security import MAX_PASSWORD_BYTES
from app.exceptions import DomainError
from app.http_errors import to_http_exception
from app.modules.auth.service import AuthService, TokenPair
from app.schemas import LoginRequest, RefreshRequest, TokenResponse

router = APIRouter()

# 5 попыток входа в минуту с одного IP — общий счётчик для /login и /token.
login_rate_limit = rate_limit(max_requests=5, window=60, scope="login")


def _login(service: AuthService, email: str, password: str) -> TokenPair:
    # Пароль длиннее лимита bcrypt не мог быть зарегистрирован: хешировать его не нужно.
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    try:
        return service.login(email, password)
    except DomainError as error:
        raise to_http_exception(error) from error


@router.post("/login", response_model=TokenResponse, dependencies=[Depends(login_rate_limit)])
def login(payload: LoginRequest, service: AuthService = Depends(get_auth_service)):
    """Аутентификация по email и паролю (JSON). Возвращает access- и refresh-токен."""
    return _login(service, payload.email, payload.password)


@router.post("/token", response_model=TokenResponse, dependencies=[Depends(login_rate_limit)])
def login_form(
    form: OAuth2PasswordRequestForm = Depends(),
    service: AuthService = Depends(get_auth_service),
):
    """
    Тот же вход в формате OAuth2 (form-data, поле username — это email).
    Его вызывает кнопка Authorize в Swagger.
    """
    return _login(service, form.username, form.password)


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, service: AuthService = Depends(get_auth_service)):
    """
    Обмен refresh-токена на новую пару. Старый refresh-токен после этого недействителен.
    Повторное использование старого токена завершает все сессии пользователя.
    """
    try:
        return service.refresh(payload.refresh_token)
    except DomainError as error:
        raise to_http_exception(error) from error
