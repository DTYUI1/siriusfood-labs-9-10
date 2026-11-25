import os

# Настройки читаются при импорте app.core.config, поэтому задаются до импорта приложения.
# Значения из .env разработчика не влияют на тесты: переменные окружения важнее файла.
os.environ["SECRET_KEY"] = "test-secret-key-for-pytest-only-0123456789"
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEBUG"] = "false"
# Сценарии сбоев почты в тестах не должны зависеть от .env демонстрации.
os.environ["EMAIL_FAIL_EVERY_NTH_ORDER"] = "5"
os.environ["DELIVERY_GRPC_TOKEN"] = "test-delivery-grpc-token-0123456789abcdef"
os.environ.pop("USERS_REPOSITORY", None)

import fakeredis  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core import redis as redis_module  # noqa: E402
from app.core.identity import RoleName  # noqa: E402
from app.core.jwt import create_access_token  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.models import MenuItem, Role  # noqa: E402
from app.modules.delivery.grpc_client import DeliveryGrpcClient, get_delivery_gateway  # noqa: E402
from app.modules.orders.service import OrderNotifier  # noqa: E402
from app.modules.users.repository import InMemoryUserRepository, SqlAlchemyUserRepository  # noqa: E402
from app.notifications.notifiers import get_order_notifier  # noqa: E402
from app.modules.users.service import User  # noqa: E402


def create_schema(engine) -> None:
    """Таблицы и справочник ролей (в рабочей БД его заполняет миграция 0002)."""
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(Role(name=role.value) for role in RoleName)
        session.commit()


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """
    Redis в памяти процесса вместо сервера: кеш и счётчики rate limit
    не переходят между тестами.
    """
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_module, "redis_client", client)
    return client


@pytest.fixture(params=["in-memory", "sqlalchemy-sqlite"])
def user_repository(request):
    """
    Обе реализации UserRepository. Тесты, использующие эту фикстуру,
    выполняются дважды и проверяют, что реализации взаимозаменяемы (LSP).
    SQLite работает в памяти процесса: сервер СУБД не нужен.
    """
    if request.param == "in-memory":
        yield InMemoryUserRepository()
        return

    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        yield SqlAlchemyUserRepository(session)
    engine.dispose()


@pytest.fixture
def session_factory():
    """Фабрика сессий SQLite в памяти. StaticPool — одна БД на все соединения теста."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    yield sessionmaker(bind=engine, autoflush=False)
    engine.dispose()


class RecordingNotifier(OrderNotifier):
    """Запоминает события вместо PDF, писем и SMS."""

    def __init__(self):
        self.events = []

    def order_created(self, event):
        self.events.append(event)


@pytest.fixture
def notifier():
    return RecordingNotifier()


@pytest.fixture
def client(session_factory, notifier):
    """Собранное приложение app.main с БД SQLite вместо PostgreSQL и заглушкой уведомлений."""
    from app.main import app

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_order_notifier] = lambda: notifier
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def create_user(session_factory):
    """Создаёт пользователя напрямую в БД. Пароль хешируется, только если он задан (bcrypt медленный)."""

    def _create(email, role=RoleName.STUDENT, balance=0.0, password=None, is_blocked=False) -> User:
        with session_factory() as db:
            return SqlAlchemyUserRepository(db).add(
                User(
                    email=email,
                    role=role,
                    balance=balance,
                    is_blocked=is_blocked,
                    password_hash=hash_password(password) if password else "!",
                )
            )

    return _create


@pytest.fixture
def create_menu_item(session_factory):
    def _create(name="Борщ", price=150.0, is_available=True) -> int:
        with session_factory() as db:
            item = MenuItem(name=name, price=price, is_available=is_available)
            db.add(item)
            db.commit()
            return item.id

    return _create


@pytest.fixture
def auth_headers():
    """Заголовок Authorization с access-токеном пользователя (без вызова /auth/login)."""

    def _headers(user: User) -> dict:
        return {"Authorization": f"Bearer {create_access_token(user.id)}"}

    return _headers


DELIVERY_TOKEN = os.environ["DELIVERY_GRPC_TOKEN"]


@pytest.fixture
def file_session_factory(tmp_path):
    """
    SQLite в файле. Для тестов, где gRPC-сервер и тест работают с БД одновременно
    (трекинг, чат): у StaticPool одно соединение на все потоки.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    create_schema(engine)
    yield sessionmaker(bind=engine, autoflush=False)
    engine.dispose()


@pytest.fixture
def delivery_server(session_factory):
    """Микросервис доставки в том же процессе, на свободном порту, с той же БД, что у монолита."""
    from delivery_service.server import create_server

    server, port = create_server(
        session_factory,
        token=DELIVERY_TOKEN,
        bind="127.0.0.1:0",
        workers=8,
        poll_seconds=0.05,
        max_track_seconds=10,
    )
    server.start()
    yield port
    server.stop(grace=None)


@pytest.fixture
def delivery_gateway(delivery_server):
    """Настоящий gRPC-клиент монолита, подключённый к тестовому микросервису."""
    gateway = DeliveryGrpcClient(f"127.0.0.1:{delivery_server}", DELIVERY_TOKEN, timeout=3)
    yield gateway
    gateway.close()


@pytest.fixture
def grpc_client(client, delivery_gateway):
    """Приложение монолита, у которого модуль delivery ходит в тестовый микросервис по gRPC."""
    from app.main import app

    app.dependency_overrides[get_delivery_gateway] = lambda: delivery_gateway
    return client
