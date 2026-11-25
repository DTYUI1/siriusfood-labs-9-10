"""Account administration. Passwords and private fields never form response DTOs."""
from app.core.identity import RoleName
from app.core.security import hash_password
from app.exceptions import NotFoundError, OperationForbiddenError, ServiceUnavailableError


class AccountConflict(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


class AccountService:
    def __init__(self, users, courier_activity=None):
        self.users = users
        self.courier_activity = courier_activity

    def _actor(self, actor):
        actual = self.users.get_by_id(actor.id)
        if actual is None or actual.is_blocked or actual.role != RoleName.ADMIN:
            raise OperationForbiddenError("Недостаточно прав")

    def get(self, actor, user_id):
        self._actor(actor)
        user = self.users.get_by_id(user_id)
        if user is None:
            raise NotFoundError("Пользователь не найден")
        return user

    def list(self, actor, **filters):
        self._actor(actor)
        return self.users.list_accounts(**filters)

    def create(self, actor, *, email, password, phone=None, role=RoleName.STUDENT, is_blocked=False):
        from app.modules.users.service import User
        password_hash = hash_password(password)
        with self.users.admin_transaction(actor.id):
            self._actor(actor)
            if self.users.get_by_email(email):
                raise AccountConflict("duplicate_email", "Email уже используется")
            return self.users.add(User(email=email, phone=phone, role=role,
                                       is_blocked=is_blocked, password_hash=password_hash))

    def update(self, actor, user_id, changes):
        with self.users.admin_transaction(actor.id, user_id):
            user = self.get(actor, user_id)
            role = changes.get("role", user.role)
            blocked = changes.get("is_blocked", user.is_blocked)
            losing_admin = user.role == RoleName.ADMIN and not user.is_blocked and (role != RoleName.ADMIN or blocked)
            if user_id == actor.id and (role != RoleName.ADMIN or blocked):
                raise AccountConflict("admin_self_guard", "Нельзя заблокировать себя или снять собственную роль администратора")
            if losing_admin and self.users.active_admin_count() <= 1:
                raise AccountConflict("last_active_admin", "Нельзя отключить последнего администратора")
            if user.role == RoleName.COURIER and (role != RoleName.COURIER or blocked):
                # User row is locked: assignment cannot slip past this read. Only
                # the owning delivery service reads deliveries; outages fail closed.
                if self.courier_activity is None:
                    raise ServiceUnavailableError("Не удалось проверить активную доставку")
                if any(d.status in ("in_progress", "on_the_way") for d in self.courier_activity(user_id)):
                    raise AccountConflict("courier_busy", "Сначала завершите активную доставку курьера")
            if "email" in changes and changes["email"] != user.email:
                existing = self.users.get_by_email(changes["email"])
                if existing and existing.id != user.id:
                    raise AccountConflict("duplicate_email", "Email уже используется")
            revoke = any(k in changes and getattr(user, k) != changes[k]
                         for k in ("email", "role", "is_blocked"))
            for key, value in changes.items():
                setattr(user, key, value)
            if revoke:
                user.auth_version += 1
            self.users.save_account(user, revoke=revoke)
            return user

    def reset_password(self, actor, user_id, password):
        password_hash = hash_password(password)
        with self.users.admin_transaction(actor.id, user_id):
            user = self.get(actor, user_id)
            user.password_hash = password_hash
            user.auth_version += 1
            self.users.save_account(user, revoke=True)
