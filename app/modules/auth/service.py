"""
Слой бизнес-логики модуля auth: вход по паролю, проверка access-токена,
обновление пары токенов с ротацией refresh-токена.
Импорты fastapi и sqlalchemy в этом файле запрещены.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from app.core.identity import CurrentUser
from app.core.jwt import (
    ACCESS,
    REFRESH,
    create_access_token,
    create_refresh_token,
    decode_token_identity,
    refresh_token_expires_at,
    utc_now,
)
from app.core.security import hash_token, verify_password
from app.exceptions import AuthenticationError, OperationForbiddenError

# Reviewed capability marker; legacy rollback also verifies the target Git bytes.
AUTH_VERSION_ENFORCEMENT = 1

if TYPE_CHECKING:
    from app.modules.auth.repository import RefreshTokenRepository
    from app.modules.users.repository import UserRepository
    from app.modules.users.service import User


@dataclass
class RefreshTokenRecord:
    """Запись о выданном refresh-токене. Сам токен не хранится — только его хеш."""

    user_id: int
    token_hash: str
    expires_at: datetime
    is_revoked: bool = False


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class AuthService:
    """
    Аутентификация: «кто ты?». Права на конкретные объекты проверяют
    сервисы модулей, роли — зависимость require_role в API-слое.
    """

    def __init__(self, users: "UserRepository", tokens: "RefreshTokenRepository"):
        self.users = users
        self.tokens = tokens

    def login(self, email: str, password: str) -> TokenPair:
        """
        Проверяет email и пароль, выдаёт пару токенов.
        Для неизвестного email и неверного пароля ответ одинаковый:
        так нельзя узнать, зарегистрирован ли email.
        """
        # Serialize password verification/token issuance with administrative reset.
        with self.users.auth_lock(email=email) as user:
            if not verify_password(password, user.password_hash if user else None):
                raise AuthenticationError("Неверный email или пароль")
            self._ensure_active(user)
            access_token, refresh_token = self._create_pair(user.id, user.auth_version)
            self.tokens.add(RefreshTokenRecord(
                user_id=user.id, token_hash=hash_token(refresh_token),
                expires_at=refresh_token_expires_at(),
            ))
            return TokenPair(access_token, refresh_token)

    def authenticate(self, access_token: str) -> CurrentUser:
        """
        Определяет пользователя по access-токену. Роль и блокировка берутся
        из БД при каждом запросе: смена роли и блокировка действуют сразу,
        а не после истечения токена.
        """
        user_id, version = decode_token_identity(access_token, ACCESS)
        user = self.users.get_by_id(user_id)
        if user is None:
            raise AuthenticationError("Пользователь не найден")
        self._ensure_version(user, version)
        self._ensure_active(user)
        return CurrentUser(id=user.id, role=user.role)

    def refresh(self, refresh_token: str) -> TokenPair:
        """
        Refresh Token rotation: старый refresh-токен отзывается, выдаётся новая пара.
        Повторное предъявление уже обменянного токена означает, что токен украден
        (им воспользовались двое). Тогда отзываются все refresh-токены пользователя:
        и злоумышленнику, и владельцу придётся войти заново по паролю.
        """
        user_id, version = decode_token_identity(refresh_token, REFRESH)
        token_hash = hash_token(refresh_token)
        with self.users.auth_lock(user_id=user_id) as user:
            if user is None:
                raise AuthenticationError("Пользователь не найден")
            self._ensure_version(user, version)
            self._ensure_active(user)
            record = self.tokens.get(token_hash)
            if record is None or record.user_id != user_id:
                raise AuthenticationError("Refresh-токен не найден")
            if record.is_revoked:
                self._revoke_all_sessions(user_id)
            if record.expires_at <= utc_now():
                raise AuthenticationError("Срок действия refresh-токена истёк")
            access_token, new_refresh_token = self._create_pair(user_id, user.auth_version)
            rotated = self.tokens.rotate(
                old_token_hash=token_hash,
                new_record=RefreshTokenRecord(
                    user_id=user_id, token_hash=hash_token(new_refresh_token),
                    expires_at=refresh_token_expires_at(),
                ),
            )
            if not rotated:
                self._revoke_all_sessions(user_id)
            return TokenPair(access_token, new_refresh_token)

    def _revoke_all_sessions(self, user_id: int) -> None:
        self.tokens.revoke_all_for_user(user_id)
        raise AuthenticationError(
            "Refresh-токен уже был использован. Все сессии завершены, войдите заново"
        )

    @staticmethod
    def _create_pair(user_id: int, auth_version: int = 0) -> tuple[str, str]:
        return create_access_token(user_id, auth_version), create_refresh_token(user_id, auth_version)

    @staticmethod
    def _ensure_version(user: "User", version: int) -> None:
        if user.auth_version != version:
            raise AuthenticationError("Сессия завершена. Войдите заново")

    @staticmethod
    def _ensure_active(user: "User") -> None:
        if user.is_blocked:
            raise OperationForbiddenError("Пользователь заблокирован")
