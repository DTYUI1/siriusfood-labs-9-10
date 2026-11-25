"""
Хеширование паролей (bcrypt) и refresh-токенов (SHA-256).
"""
import hashlib
from functools import cache

from passlib.context import CryptContext
from passlib.exc import UnknownHashError

# bcrypt — медленный алгоритм с солью: одинаковые пароли дают разные хеши,
# а перебор по утёкшей базе стоит дорого.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

MIN_PASSWORD_LENGTH = 8
# bcrypt учитывает только первые 72 байта пароля, остальное молча отбрасывает.
MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    """Возвращает bcrypt-хеш пароля."""
    return pwd_context.hash(password)


@cache
def _dummy_hash() -> str:
    return pwd_context.hash("dummy-password-for-timing")


def verify_password(plain: str, hashed: str | None) -> bool:
    """
    Проверяет соответствие пароля хешу. hashed=None — пользователь не найден:
    хеш всё равно вычисляется, чтобы по времени ответа нельзя было понять,
    существует ли email. Нераспознанный хеш (например, '!' у пользователей,
    перенесённых миграцией) не совпадает ни с одним паролем.
    """
    if hashed is None:
        pwd_context.verify(plain, _dummy_hash())
        return False
    try:
        return pwd_context.verify(plain, hashed)
    except UnknownHashError:
        return False


def hash_token(token: str) -> str:
    """
    SHA-256 refresh-токена для хранения в БД. bcrypt здесь не нужен: токен —
    случайная строка большой длины, перебрать её нельзя, а по быстрому хешу
    запись находится индексом.
    """
    return hashlib.sha256(token.encode()).hexdigest()
