"""
Инфраструктурный слой модуля auth (круг 3): хранение выданных refresh-токенов.
"""
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import RefreshToken
from app.modules.auth.service import RefreshTokenRecord


class RefreshTokenRepository(ABC):
    """Абстрактное хранилище refresh-токенов."""

    @abstractmethod
    def add(self, record: RefreshTokenRecord) -> None:
        """Сохранение выданного токена."""

    @abstractmethod
    def get(self, token_hash: str) -> RefreshTokenRecord | None:
        """Поиск записи по хешу токена."""

    @abstractmethod
    def rotate(self, old_token_hash: str, new_record: RefreshTokenRecord) -> bool:
        """
        Атомарно отзывает старый токен и сохраняет новый. Возвращает False,
        если старый токен уже отозван (его успел обменять другой запрос).
        """

    @abstractmethod
    def revoke_all_for_user(self, user_id: int) -> None:
        """Отзыв всех refresh-токенов пользователя (сброс всех сессий)."""

    @abstractmethod
    def delete_expired(self, now: datetime) -> int:
        """
        Удаление истёкших токенов. Отозванные, но не истёкшие, остаются:
        по ним AuthService узнаёт повторное использование украденного токена.
        Возвращает число удалённых записей.
        """


class SqlAlchemyRefreshTokenRepository(RefreshTokenRepository):
    """Хранение refresh-токенов в таблице refresh_tokens."""

    def __init__(self, db: Session):
        self.db = db

    def add(self, record: RefreshTokenRecord) -> None:
        self.db.add(self._to_row(record))
        self.db.commit()

    def get(self, token_hash: str) -> RefreshTokenRecord | None:
        row = self.db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()
        if row is None:
            return None
        return RefreshTokenRecord(
            user_id=row.user_id,
            token_hash=row.token_hash,
            expires_at=row.expires_at,
            is_revoked=row.is_revoked,
        )

    def rotate(self, old_token_hash: str, new_record: RefreshTokenRecord) -> bool:
        # Условный UPDATE: из двух одновременных запросов с одним токеном
        # строку изменит только один, второй получит rowcount == 0.
        revoked = (
            self.db.query(RefreshToken)
            .filter(RefreshToken.token_hash == old_token_hash, RefreshToken.is_revoked.is_(False))
            .update({RefreshToken.is_revoked: True})
        )
        if revoked != 1:
            self.db.rollback()
            return False
        self.db.add(self._to_row(new_record))
        self.db.commit()
        return True

    def revoke_all_for_user(self, user_id: int) -> None:
        self.db.query(RefreshToken).filter(RefreshToken.user_id == user_id).update(
            {RefreshToken.is_revoked: True}
        )
        self.db.commit()

    def delete_expired(self, now: datetime) -> int:
        deleted = self.db.query(RefreshToken).filter(RefreshToken.expires_at < now).delete()
        self.db.commit()
        return deleted

    @staticmethod
    def _to_row(record: RefreshTokenRecord) -> RefreshToken:
        return RefreshToken(
            user_id=record.user_id,
            token_hash=record.token_hash,
            expires_at=record.expires_at,
            is_revoked=record.is_revoked,
        )


class InMemoryRefreshTokenRepository(RefreshTokenRepository):
    """Хранение refresh-токенов в словаре (unit-тесты, режим USERS_REPOSITORY=memory)."""

    def __init__(self):
        self._records: dict[str, RefreshTokenRecord] = {}

    def add(self, record: RefreshTokenRecord) -> None:
        self._records[record.token_hash] = replace(record)

    def get(self, token_hash: str) -> RefreshTokenRecord | None:
        record = self._records.get(token_hash)
        return replace(record) if record else None

    def rotate(self, old_token_hash: str, new_record: RefreshTokenRecord) -> bool:
        old = self._records.get(old_token_hash)
        if old is None or old.is_revoked:
            return False
        old.is_revoked = True
        self.add(new_record)
        return True

    def revoke_all_for_user(self, user_id: int) -> None:
        for record in self._records.values():
            if record.user_id == user_id:
                record.is_revoked = True

    def delete_expired(self, now: datetime) -> int:
        expired = [token_hash for token_hash, record in self._records.items() if record.expires_at < now]
        for token_hash in expired:
            del self._records[token_hash]
        return len(expired)
