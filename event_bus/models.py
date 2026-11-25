"""Общие таблицы inbox/outbox. Их схему ведёт Alembic монолита."""
from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Outbox(Base):
    __tablename__ = "outbox"

    id = Column(String(36), primary_key=True)
    dedup_key = Column(String(160), unique=True, nullable=False)
    routing_key = Column(String(100), nullable=False)
    payload = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    published_at = Column(DateTime, nullable=True, index=True)


class ProcessedEvent(Base):
    __tablename__ = "processed_events"

    # Один event_id обязан обработаться ОТДЕЛЬНО каждым consumer'ом.
    consumer = Column(String(50), primary_key=True)
    event_id = Column(String(36), primary_key=True)
    processed_at = Column(DateTime, nullable=False, default=utcnow)
