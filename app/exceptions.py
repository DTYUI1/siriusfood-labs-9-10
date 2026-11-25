"""
Доменные исключения бизнес-логики.

Модуль не зависит от фреймворков: сервисы сообщают через эти исключения
о нарушении бизнес-правил, а транспортный слой (router.py) переводит
их в HTTP-статусы. Поэтому сервис ничего не знает о кодах 400/403/404.
"""


class DomainError(Exception):
    """Базовое исключение слоя бизнес-логики."""


class NotFoundError(DomainError):
    """Объект предметной области не найден."""


class BusinessRuleError(DomainError):
    """Нарушено бизнес-правило предметной области."""


class ConflictError(BusinessRuleError):
    """Requested transition conflicts with the current committed state."""
    def __init__(self, message, code="state_conflict"):
        super().__init__(message)
        self.code = code


class OperationForbiddenError(DomainError):
    """Операция запрещена для данного объекта (например, пользователь заблокирован)."""


class AccessDeniedError(OperationForbiddenError):
    """У пользователя нет прав на этот объект (чужой заказ, чужая доставка)."""


class AuthenticationError(DomainError):
    """Не удалось установить, кто выполняет запрос: неверный пароль или токен."""


class ServiceUnavailableError(DomainError):
    """Внешний сервис недоступен (например, микросервис доставки не запущен)."""


class ServiceTimeoutError(ServiceUnavailableError):
    """Внешний сервис не ответил за отведённое время."""
