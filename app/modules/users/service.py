"""
Слой бизнес-логики модуля users (круги 1–2: Entities и Use Cases).
Импорты fastapi и sqlalchemy в этом файле запрещены.
"""
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.core.identity import CurrentUser, RoleName
from app.core.security import hash_password
from app.exceptions import AccessDeniedError, BusinessRuleError, ConflictError, NotFoundError, OperationForbiddenError, ServiceUnavailableError

if TYPE_CHECKING:
    # Импорт только для аннотаций типов. Во время выполнения service.py
    # не зависит от repository.py: наоборот, repository.py импортирует
    # отсюда сущность User (зависимости направлены снаружи внутрь).
    from app.modules.users.repository import UserRepository

MAX_TOP_UP_AMOUNT = 50000


@dataclass
class User:
    """
    Сущность «Пользователь» (круг 1). Не связана ни с ORM, ни с HTTP.
    Новый пользователь создаётся с нулевым балансом, без блокировки и с ролью student.
    """

    email: str
    phone: str | None = None
    balance: float = 0.0
    is_blocked: bool = False
    # repr=False: хеш пароля не попадает в логи и тексты ошибок.
    password_hash: str = field(default="", repr=False)
    role: RoleName = RoleName.STUDENT
    id: int | None = None
    auth_version: int = field(default=0, repr=False)

    def top_up(self, amount: float) -> None:
        """Пополнение баланса с проверкой правил предметной области."""
        if self.is_blocked:
            raise OperationForbiddenError("Пользователь заблокирован. Пополнение невозможно")

        if amount > MAX_TOP_UP_AMOUNT:
            raise BusinessRuleError(f"Максимальная сумма пополнения — {MAX_TOP_UP_AMOUNT} рублей")

        self.balance += amount


class UserService:
    """
    Сценарии использования модуля users (круг 2).
    Не имеет зависимостей от инфраструктурных фреймворков.
    """

    def __init__(self, repository: "UserRepository", courier_activity=None):
        # Внедрение зависимости через конструктор: сервис работает
        # с любой реализацией абстрактного UserRepository.
        self.repository = repository
        self.courier_activity = courier_activity

    def register_user(self, email: str, password: str, phone: str | None = None) -> User:
        """
        Регистрация пользователя. Пароль сохраняется только в виде bcrypt-хеша.
        Роль всегда student: клиент не может выбрать себе роль при регистрации.
        Возвращает созданную сущность с id. Возбуждает BusinessRuleError, если email уже занят.
        """
        if self.repository.get_by_email(email):
            raise BusinessRuleError("Пользователь с таким email уже зарегистрирован")

        return self.repository.add(
            User(
                email=email,
                phone=phone,
                password_hash=hash_password(password),
                role=RoleName.STUDENT,
            )
        )

    def get_profile(self, user_id: int) -> User:
        """Профиль пользователя. Возбуждает NotFoundError, если его нет."""
        user = self.repository.get_by_id(user_id)
        if not user:
            raise NotFoundError("Пользователь не найден")
        return user

    def top_up_balance(self, user_id: int, amount: float, current_user: CurrentUser) -> float:
        """
        Пополнение баланса пользователя. Возвращает новый баланс.
        Пополнить можно только свой баланс; администратор — любой.
        Возбуждает DomainError при нарушении бизнес-правил.
        """
        # Права проверяются раньше поиска: иначе по разнице 403/404 можно
        # было бы перебором узнать, какие id пользователей существуют.
        if user_id != current_user.id and not current_user.is_admin:
            raise AccessDeniedError("Нельзя пополнить баланс другого пользователя")

        user = self.get_profile(user_id)
        user.top_up(amount)
        self.repository.update_balance(user.id, user.balance)
        return user.balance

    def change_role(self, user_id: int, role: RoleName) -> User:
        """Operator CLI/bootstrap path; HTTP uses the actor-aware AccountService."""
        with self.repository.admin_transaction(user_id, user_id):
            user = self.get_profile(user_id)
            if user.role == role:
                return user
            if user.role == RoleName.ADMIN and not user.is_blocked and self.repository.active_admin_count() <= 1:
                raise ConflictError("Нельзя снять роль последнего администратора", "last_active_admin")
            if user.role == RoleName.COURIER:
                if self.courier_activity is None:
                    raise ServiceUnavailableError("Не удалось проверить активную доставку")
                if any(d.status in ("in_progress", "on_the_way") for d in self.courier_activity(user_id)):
                    raise ConflictError("Сначала завершите активную доставку курьера", "courier_busy")
            self.repository.update_role(user.id, role)
            user.role, user.auth_version = role, user.auth_version + 1
            return user

    def set_password(self, email: str, password: str) -> None:
        """Назначение нового пароля (CLI администратора). Возбуждает NotFoundError."""
        user = self.repository.get_by_email(email)
        if not user:
            raise NotFoundError("Пользователь не найден")
        self.repository.update_password(user.id, hash_password(password))
