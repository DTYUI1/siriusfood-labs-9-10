"""
Зависимости FastAPI для безопасности (транспортный слой).

get_current_user — аутентификация: «кто ты?» (401, если токен невалиден).
require_role — авторизация по роли: «пускают ли тебя на этот эндпоинт?» (403).
Права на конкретный объект («твой ли это заказ?») проверяют сервисы модулей.
"""
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.identity import CurrentUser, RoleName
from app.database import get_db
from app.exceptions import DomainError
from app.http_errors import to_http_exception
from app.modules.auth.repository import RefreshTokenRepository, SqlAlchemyRefreshTokenRepository
from app.modules.auth.service import AuthService
from app.modules.users.repository import SqlAlchemyUserRepository, UserRepository

# Токен извлекается из заголовка Authorization: Bearer <token>.
# tokenUrl — эндпоинт, который вызывает кнопка Authorize в Swagger.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


def get_user_repository(db: Session = Depends(get_db)) -> UserRepository:
    """
    Реализация репозитория пользователей для всего приложения: модуля users,
    входа и проверки токена. Подменяется через app.dependency_overrides.
    """
    return SqlAlchemyUserRepository(db)


def get_refresh_token_repository(db: Session = Depends(get_db)) -> RefreshTokenRepository:
    return SqlAlchemyRefreshTokenRepository(db)


def get_auth_service(
    users: UserRepository = Depends(get_user_repository),
    tokens: RefreshTokenRepository = Depends(get_refresh_token_repository),
) -> AuthService:
    return AuthService(users, tokens)


def get_current_user(
    token: str = Depends(oauth2_scheme),
    service: AuthService = Depends(get_auth_service),
) -> CurrentUser:
    """
    Текущий пользователь по access-токену. id берётся из подписанного сервером
    JWT, а не из тела запроса: подделать его без SECRET_KEY нельзя.
    401 — токен невалиден или просрочен, 403 — пользователь заблокирован.
    """
    try:
        return service.authenticate(token)
    except DomainError as error:
        raise to_http_exception(error) from error


def require_role(*allowed_roles: RoleName):
    """
    Dependency для проверки роли. Возбуждает 403, если роль текущего
    пользователя не входит в allowed_roles.
    """

    def checker(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current_user.role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Недостаточно прав для этого действия")
        return current_user

    return checker
