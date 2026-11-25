"""
Трансляция доменных исключений в HTTP-ответы (транспортный слой).
"""
from fastapi import HTTPException

from app.exceptions import (
    AuthenticationError,
    BusinessRuleError,
    ConflictError,
    DomainError,
    NotFoundError,
    OperationForbiddenError,
    ServiceTimeoutError,
    ServiceUnavailableError,
)

_STATUS_CODES = {
    # Порядок важен: ServiceTimeoutError — подкласс ServiceUnavailableError.
    ServiceTimeoutError: 504,
    ServiceUnavailableError: 503,
    AuthenticationError: 401,
    NotFoundError: 404,
    OperationForbiddenError: 403,
    ConflictError: 409,
    BusinessRuleError: 400,
}


def to_http_exception(error: DomainError) -> HTTPException:
    """Возвращает HTTPException с кодом, соответствующим типу доменной ошибки."""
    status_code = next(
        (code for error_type, code in _STATUS_CODES.items() if isinstance(error, error_type)),
        400,
    )
    # RFC 6750: ответ 401 сообщает клиенту, что нужен Bearer-токен.
    headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
    detail = {"code": error.code, "message": str(error)} if isinstance(error, ConflictError) else str(error)
    return HTTPException(status_code=status_code, detail=detail, headers=headers)
