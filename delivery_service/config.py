"""
Настройки микросервиса доставки. Читаются из окружения и того же .env, что у монолита.
"""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeliverySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Пока база общая с монолитом. Следующий шаг Strangler Fig — своя БД доставки.
    RABBITMQ_URL: str = "amqp://sirius:sirius@127.0.0.1:5672/"
    DATABASE_URL: str
    # Служебный токен: без него сервер отвечает UNAUTHENTICATED.
    # Общий секрет монолита и сервиса; в продакшене — mTLS.
    DELIVERY_GRPC_TOKEN: str = Field(min_length=32)
    # По умолчанию только localhost: соединение без TLS не должно быть доступно из сети.
    DELIVERY_GRPC_BIND: str = "127.0.0.1:50051"
    # Каждый открытый поток (трекинг, чат) занимает поток пула.
    DELIVERY_GRPC_WORKERS: int = 32
    # Как часто TrackDelivery перечитывает статус и сколько живёт поток трекинга.
    TRACK_POLL_SECONDS: float = 1.0
    TRACK_MAX_SECONDS: int = 3600


settings = DeliverySettings()
