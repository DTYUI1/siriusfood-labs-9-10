"""
Кто выполняет запрос: роль и идентификатор текущего пользователя.

Модуль не зависит от фреймворков, поэтому сервисы получают CurrentUser
и проверяют по нему права на объект, ничего не зная о JWT и HTTP.
"""
from dataclasses import dataclass
from enum import StrEnum


class RoleName(StrEnum):
    """Роли «Сириус.Еды». Значения совпадают со строками таблицы roles."""

    STUDENT = "student"
    COOK = "cook"
    COURIER = "courier"
    ADMIN = "admin"


@dataclass(frozen=True)
class CurrentUser:
    """Аутентифицированный пользователь. id взят из подписанного JWT, роль — из БД."""

    id: int
    role: RoleName

    @property
    def is_admin(self) -> bool:
        return self.role == RoleName.ADMIN
