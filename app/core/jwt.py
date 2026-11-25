"""
Создание и проверка JWT. Payload не шифруется, а только кодируется в Base64:
в нём нет ничего, кроме идентификатора пользователя и служебных полей.
"""
import uuid
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from app.core.config import settings
from app.exceptions import AuthenticationError

ALGORITHM = "HS256"
ACCESS = "access"
REFRESH = "refresh"


def _create_token(user_id: int, token_type: str, lifetime: timedelta, auth_version: int = 0) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "ver": auth_version,
        "type": token_type,
        "iat": now,
        "exp": now + lifetime,
        # Уникальный id: два токена, выданные в одну секунду, всё равно различаются.
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def create_access_token(user_id: int, auth_version: int = 0) -> str:
    """Короткоживущий access-токен (15 минут): если украдут — быстро сгорит."""
    return _create_token(user_id, ACCESS, timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES), auth_version)


def create_refresh_token(user_id: int, auth_version: int = 0) -> str:
    """Долгоживущий refresh-токен (7 дней): нужен только для получения новой пары."""
    return _create_token(user_id, REFRESH, timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS), auth_version)


def decode_token(token: str, expected_type: str) -> int:
    """
    Проверяет подпись, срок действия и тип токена. Возвращает id пользователя.
    Тип проверяется отдельно: иначе refresh-токен подошёл бы вместо access.
    Возбуждает AuthenticationError, если токен невалиден.
    """
    return decode_token_identity(token, expected_type)[0]


def decode_token_identity(token: str, expected_type: str) -> tuple[int, int]:
    """Legacy signed tokens without ver are valid only for untouched version-zero accounts."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != expected_type:
            raise AuthenticationError("Неверный тип токена")
        version = payload.get("ver", 0)
        if type(version) is not int or version < 0:
            raise AuthenticationError("Невалидная версия сессии")
        return int(payload["sub"]), version
    except (JWTError, KeyError, ValueError) as error:
        raise AuthenticationError("Невалидный или просроченный токен") from error


def refresh_token_expires_at() -> datetime:
    """Момент истечения нового refresh-токена в UTC (без часового пояса, как в колонке БД)."""
    return utc_now() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)


def utc_now() -> datetime:
    """Текущее время UTC без часового пояса — в таком виде время хранится в БД."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
