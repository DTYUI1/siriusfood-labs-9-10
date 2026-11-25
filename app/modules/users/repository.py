"""
Инфраструктурный слой модуля users (круг 3: Interface Adapters).

UserRepository — абстрактный интерфейс хранилища, с которым работает сервис.
Реализации:
- SqlAlchemyUserRepository — PostgreSQL через SQLAlchemy ORM (используется в API);
- InMemoryUserRepository — словарь в памяти (unit-тесты, работа без СУБД).
Обе реализации принимают и возвращают доменную сущность User, а не ORM-модель.
"""
from abc import ABC, abstractmethod
from dataclasses import replace
from contextlib import contextmanager
from threading import RLock

from sqlalchemy.orm import Session

from app.core.identity import RoleName
from app.core.auth_serialization import lock_auth_mutation
from app.models import Role as RoleModel
from app.models import User as UserModel
from app.models import RefreshToken
from app.modules.users.service import User


class UserRepository(ABC):
    """Абстрактный репозиторий пользователей. Бизнес-правила здесь отсутствуют."""

    @abstractmethod
    def get_by_id(self, user_id: int) -> User | None:
        """Поиск пользователя по идентификатору."""

    @abstractmethod
    def get_by_email(self, email: str) -> User | None:
        """Поиск пользователя по email."""

    @abstractmethod
    def add(self, user: User) -> User:
        """Сохранение нового пользователя. Возвращает его с присвоенным id."""

    @abstractmethod
    def update_balance(self, user_id: int, balance: float) -> None:
        """Запись нового значения баланса пользователя."""

    @abstractmethod
    def update_role(self, user_id: int, role: RoleName) -> None:
        """Запись новой роли пользователя."""

    @abstractmethod
    def update_password(self, user_id: int, password_hash: str) -> None:
        """Запись нового хеша пароля."""

    @contextmanager
    def auth_lock(self, *, user_id=None, email=None):
        """Implementations serialize issuance with reset/role/block mutations."""
        raise NotImplementedError
        yield


class SqlAlchemyUserRepository(UserRepository):
    """Хранение пользователей в PostgreSQL через SQLAlchemy ORM."""

    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, user_id: int) -> User | None:
        row = self.db.query(UserModel).filter(UserModel.id == user_id).first()
        return self._to_entity(row) if row else None

    def get_by_email(self, email: str) -> User | None:
        row = self.db.query(UserModel).filter(UserModel.email == email).first()
        return self._to_entity(row) if row else None

    @contextmanager
    def auth_lock(self, *, user_id=None, email=None):
        query = self.db.query(UserModel).populate_existing()
        query = query.filter(UserModel.id == user_id) if user_id is not None else query.filter(UserModel.email == email)
        row = query.with_for_update(of=UserModel).first()
        yield self._to_entity(row) if row else None

    @contextmanager
    def admin_transaction(self, actor_id, target_id=None):
        try:
            lock_auth_mutation(self.db)
            # A shared, stable row serializes *all* role/block mutations, including
            # two administrators demoting each other. Lock target users in id order.
            self.db.query(RoleModel).filter_by(name=RoleName.ADMIN.value).with_for_update().one()
            ids = sorted({i for i in (actor_id, target_id) if i is not None})
            self.db.query(UserModel).filter(UserModel.id.in_(ids)).order_by(UserModel.id).populate_existing().with_for_update(of=UserModel).all()
            yield
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def list_accounts(self, *, offset, limit, q=None, role=None, is_blocked=None):
        query = self.db.query(UserModel).join(RoleModel)
        if q:
            query = query.filter(UserModel.email.ilike(f"%{q}%", escape="\\"))
        if role is not None:
            query = query.filter(RoleModel.name == role.value)
        if is_blocked is not None:
            query = query.filter(UserModel.is_blocked == is_blocked)
        total = query.count()
        return [self._to_entity(row) for row in query.order_by(UserModel.id).offset(offset).limit(limit)], total

    def active_admin_count(self):
        return self.db.query(UserModel).join(RoleModel).filter(RoleModel.name == "admin", UserModel.is_blocked.is_(False)).count()

    def save_account(self, user, *, revoke=False):
        if revoke:
            lock_auth_mutation(self.db)
        row = self.db.get(UserModel, user.id)
        row.email, row.phone, row.role_id = user.email, user.phone, self._role(user.role).id
        row.is_blocked, row.password_hash = user.is_blocked, user.password_hash
        row.auth_version = user.auth_version
        if revoke:
            self.db.query(RefreshToken).filter_by(user_id=user.id).update({RefreshToken.is_revoked: True})
        self.db.flush()

    def add(self, user: User) -> User:
        row = UserModel(
            email=user.email,
            phone=user.phone,
            balance=user.balance,
            is_blocked=user.is_blocked,
            password_hash=user.password_hash,
            role=self._role(user.role),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return self._to_entity(row)

    def update_balance(self, user_id: int, balance: float) -> None:
        self.db.query(UserModel).filter(UserModel.id == user_id).update({UserModel.balance: balance})
        self.db.commit()

    def update_role(self, user_id: int, role: RoleName) -> None:
        lock_auth_mutation(self.db)
        self.db.query(UserModel).filter(UserModel.id == user_id).update(
            {UserModel.role_id: self._role(role).id, UserModel.auth_version: UserModel.auth_version + 1}
        )
        self.db.query(RefreshToken).filter_by(user_id=user_id).update({RefreshToken.is_revoked: True})
        self.db.commit()

    def update_password(self, user_id: int, password_hash: str) -> None:
        lock_auth_mutation(self.db)
        self.db.query(UserModel).filter(UserModel.id == user_id).update(
            {UserModel.password_hash: password_hash, UserModel.auth_version: UserModel.auth_version + 1}
        )
        self.db.query(RefreshToken).filter_by(user_id=user_id).update({RefreshToken.is_revoked: True})
        self.db.commit()

    def _role(self, name: RoleName) -> RoleModel:
        """Строка справочника roles. Справочник заполняет миграция 0002."""
        role = self.db.query(RoleModel).filter(RoleModel.name == name.value).first()
        if role is None:
            raise LookupError(f"Роли {name.value!r} нет в таблице roles: выполните alembic upgrade head")
        return role

    @staticmethod
    def _to_entity(row: UserModel) -> User:
        """Преобразование ORM-модели в доменную сущность."""
        return User(
            id=row.id,
            email=row.email,
            phone=row.phone,
            balance=row.balance,
            is_blocked=row.is_blocked,
            password_hash=row.password_hash,
            role=RoleName(row.role.name),
            auth_version=row.auth_version,
        )


class InMemoryUserRepository(UserRepository):
    """
    Хранение пользователей в словаре. Наружу отдаются копии сущностей,
    поэтому изменения попадают в хранилище только через методы
    репозитория — так же, как при работе с СУБД.
    """

    def __init__(self):
        self._users: dict[int, User] = {}
        self._next_id = 1
        self._lock = RLock()

    @contextmanager
    def auth_lock(self, *, user_id=None, email=None):
        with self._lock:
            yield self.get_by_id(user_id) if user_id is not None else self.get_by_email(email)

    @contextmanager
    def admin_transaction(self, actor_id, target_id=None):
        with self._lock:
            yield

    def list_accounts(self, *, offset, limit, q=None, role=None, is_blocked=None):
        users = [replace(u) for u in self._users.values()
                 if (not q or q.lower() in u.email.lower())
                 and (role is None or u.role == role)
                 and (is_blocked is None or u.is_blocked == is_blocked)]
        users.sort(key=lambda u: u.id)
        return users[offset:offset + limit], len(users)

    def active_admin_count(self):
        return sum(u.role == RoleName.ADMIN and not u.is_blocked for u in self._users.values())

    def save_account(self, user, *, revoke=False):
        self._users[user.id] = replace(user)

    def get_by_id(self, user_id: int) -> User | None:
        user = self._users.get(user_id)
        return replace(user) if user else None

    def get_by_email(self, email: str) -> User | None:
        return next((replace(user) for user in self._users.values() if user.email == email), None)

    def add(self, user: User) -> User:
        stored = replace(user, id=self._next_id)
        self._users[stored.id] = stored
        self._next_id += 1
        return replace(stored)

    def update_balance(self, user_id: int, balance: float) -> None:
        self._users[user_id].balance = balance

    def update_role(self, user_id: int, role: RoleName) -> None:
        with self._lock:
            self._users[user_id].role = role
            self._users[user_id].auth_version += 1

    def update_password(self, user_id: int, password_hash: str) -> None:
        with self._lock:
            self._users[user_id].password_hash = password_hash
            self._users[user_id].auth_version += 1
