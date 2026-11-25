"""
Настройки приложения. Значения читаются из переменных окружения и файла .env.

Secure by Default: у SECRET_KEY и DATABASE_URL нет значений по умолчанию,
поэтому без них приложение не запустится. DEBUG по умолчанию выключен:
Swagger и вывод SQL нужно включать явно.
"""
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки приложения, загружаемые из окружения и .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Ключ подписи JWT. Короткий ключ HS256 подбирается перебором, поэтому не меньше 32 символов.
    SECRET_KEY: str = Field(min_length=32)
    DATABASE_URL: str
    REDIS_URL: str = "redis://localhost:6379"
    DEBUG: bool = False
    # Пустой список — браузерные запросы с чужих доменов запрещены.
    ALLOWED_ORIGINS: list[str] = []

    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # False — кеш выключен, все чтения идут в БД: замер «до» на том же коде.
    CACHE_ENABLED: bool = True
    # TTL кеша меню, секунды. Меню меняется редко, а изменения сбрасывают кеш сразу,
    # поэтому TTL — страховка на случай пропущенной инвалидации.
    MENU_CACHE_TTL: int = 300
    # Категории меняются ещё реже, чем блюда в них.
    CATEGORY_CACHE_TTL: int = 600
    # «Категории нет» тоже кешируется, но ненадолго: перебор несуществующих id не нагружает БД.
    NOT_FOUND_CACHE_TTL: int = 60

    # Уведомления о заказе (PDF, email, SMS): celery — фоновые задачи,
    # sync — в обработчике запроса (замер «до» на том же коде).
    ORDER_NOTIFICATIONS: Literal["celery", "sync"] = "celery"
    # Брокер и хранилище результатов Celery. По умолчанию — тот же Redis.
    CELERY_BROKER_URL: str | None = None
    CELERY_RESULT_BACKEND: str | None = None
    # Часовой пояс, по которому считаются сутки в статистике заказов.
    STATS_TIMEZONE: str = "Europe/Moscow"
    # Каждый N-й заказ имитирует недоступность почты (0 — никогда).
    EMAIL_FAIL_EVERY_NTH_ORDER: int = 5

    # События ЛР7. Пароль задаётся через .env, не в коде.
    RABBITMQ_URL: str = "amqp://sirius:sirius@127.0.0.1:5672/"

    # Микросервис доставки (gRPC). Токен — общий секрет монолита и сервиса,
    # без него сервис отвечает UNAUTHENTICATED.
    DELIVERY_GRPC_TARGET: str = "127.0.0.1:50051"
    DELIVERY_GRPC_TOKEN: str = Field(min_length=32)
    # Дедлайн одного вызова, секунды: упавший сервис не держит HTTP-запрос дольше.
    DELIVERY_GRPC_TIMEOUT: float = 3.0


settings = Settings()
