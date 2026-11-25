"""
Точка входа приложения. Выполняет инициализацию FastAPI и подключение модулей.
Таблицы базы данных создаются миграциями: alembic upgrade head.
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.responses import PlainTextResponse
from sqlalchemy.exc import SQLAlchemyError
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.http_logging import install_http_log_privacy
from app.core.observability import HEADER, ObservabilityMiddleware, metrics_response
from app.core import redis as redis_module
from app.core.dependencies import get_refresh_token_repository, get_user_repository
from app.database import SessionLocal, engine
from app.modules.auth.repository import InMemoryRefreshTokenRepository
from app.modules.auth.router import router as auth_router
from app.modules.users.repository import InMemoryUserRepository
from app.modules.users.router import router as users_router
from app.modules.menu.repository import MenuRepository
from app.modules.menu.router import router as menu_router
from app.modules.orders.router import admin_router as admin_orders_router, kitchen_router, router as orders_router
from app.modules.delivery.admin_router import router as admin_delivery_router
from app.modules.search.router import router as search_router
from app.modules.assistant.router import router as assistant_router
from app.modules.admin_accounts.router import router as admin_accounts_router
from app.modules.delivery.grpc_client import close_delivery_gateway
from app.modules.delivery.router import router as delivery_router

from event_bus.observability import configure_json_logging

configure_json_logging("api", uvicorn=True)
install_http_log_privacy()
logger = logging.getLogger(__name__)


def warm_up_menu_cache() -> None:
    """
    Прогрев кеша меню (cache warm-up): после деплоя первый обеденный запрос
    не идёт в БД. Если БД или Redis недоступны, приложение всё равно стартует.
    """
    cache = redis_module.get_cache()
    if cache is None:
        return
    try:
        with SessionLocal() as db:
            items = MenuRepository(db, cache).warm_up()
    except SQLAlchemyError as error:
        logger.warning("Кеш меню не прогрет: %s", error)
        return
    logger.info("Кеш меню прогрет: %s блюд", items)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    warm_up_menu_cache()
    yield
    # Канал к микросервису доставки закрывается вместе с приложением.
    close_delivery_gateway()


# Swagger раскрывает структуру API, поэтому он доступен только при DEBUG=True.
# openapi_url тоже отключается: иначе схема API осталась бы доступна по /openapi.json.
app = FastAPI(
    title="Sirius.Food API",
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
    lifespan=lifespan,
)

# CORS: браузер разрешит запросы к API только со страниц доменов из ALLOWED_ORIGINS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Authorization", "Content-Type", HEADER],
    expose_headers=[HEADER],
)
app.add_middleware(ObservabilityMiddleware)


@app.exception_handler(Exception)
def unhandled_error(request: Request, _error: Exception):
    # ServerErrorMiddleware формирует ответ вне middleware приложения.
    headers = {HEADER: request.state.correlation_id} if hasattr(request.state, "correlation_id") else {}
    return PlainTextResponse("Internal Server Error", status_code=500, headers=headers)


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics():
    return metrics_response(engine.pool)

app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(users_router, prefix="/users", tags=["users"])
app.include_router(menu_router, prefix="/menu", tags=["menu"])
app.include_router(orders_router, prefix="/orders", tags=["orders"])
app.include_router(kitchen_router, prefix="/kitchen", tags=["kitchen"])
app.include_router(delivery_router, prefix="/delivery", tags=["delivery"])
app.include_router(admin_orders_router, prefix="/admin", tags=["admin"])
app.include_router(admin_delivery_router, prefix="/admin", tags=["admin"])
app.include_router(admin_accounts_router, prefix="/admin", tags=["admin"])
app.include_router(search_router, prefix="/menu", tags=["search"])
app.include_router(assistant_router, prefix="/assistant", tags=["assistant"])

# Выбор реализации репозитория модуля users: sqlalchemy (PostgreSQL, по умолчанию)
# или memory (словарь в памяти процесса, данные пропадают при перезапуске).
# Замена выполняется только здесь — service.py и router.py не меняются.
# Вход и проверка токена читают пользователей через тот же репозиторий,
# поэтому в режиме memory в памяти хранятся и выданные refresh-токены.
USERS_REPOSITORY = os.getenv("USERS_REPOSITORY", "sqlalchemy")

if USERS_REPOSITORY == "memory":
    users_repository = InMemoryUserRepository()
    tokens_repository = InMemoryRefreshTokenRepository()
    app.dependency_overrides[get_user_repository] = lambda: users_repository
    app.dependency_overrides[get_refresh_token_repository] = lambda: tokens_repository
elif USERS_REPOSITORY != "sqlalchemy":
    raise ValueError(f"USERS_REPOSITORY={USERS_REPOSITORY!r}: допустимы 'sqlalchemy' или 'memory'")

# Opt-in only: author/integrated runtime explicitly points at the matching build.
from app.core.product_config import product_settings
from app.core.product_web import mount_frontend

mount_frontend(app, product_settings().frontend_dist)
