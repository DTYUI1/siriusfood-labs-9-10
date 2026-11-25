"""
Модуль инициализации подключения к СУБД PostgreSQL.
Схема БД создаётся миграциями Alembic (alembic upgrade head).
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from event_bus.models import Base

from app.core.config import settings

# При DEBUG=True SQL-запросы (INSERT/UPDATE) выводятся в терминал uvicorn.
# В продакшене вывод выключен: в логи не попадают данные пользователей.
# Для SQLite (запуск без Docker) разрешаем работу сессии из разных потоков.
engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    connect_args={"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """Генератор сессии базы данных для использования в Depends."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
