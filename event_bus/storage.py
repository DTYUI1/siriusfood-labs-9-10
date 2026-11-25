"""Запись событий в транзакцию бизнес-операции и отправка после commit."""
import json
import logging
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from event_bus.contracts import new_event
from event_bus.models import Outbox, utcnow
from event_bus.observability import correlation_scope, event_correlation_id, event_log_fields

logger = logging.getLogger(__name__)


def make_session_factory(url):
    engine = create_engine(
        url, pool_pre_ping=True,
        connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
    )
    return sessionmaker(bind=engine, autoflush=False)


def append_event(db, routing_key, order_id, customer_id, amount, **extra):
    payload = new_event(routing_key, order_id, customer_id, amount, **extra)
    db.add(Outbox(
        id=payload["event_id"],
        dedup_key=f"{routing_key}:{order_id}",
        routing_key=routing_key,
        payload=json.dumps(payload, ensure_ascii=False, allow_nan=False),
    ))
    return payload


async def publish_batch(session_factory, bus, limit=50):
    """Confirms + mandatory; пометка published только после подтверждения брокера.

    Сбой после publish до commit может дать повтор: inbox делает его безопасным.
    SKIP LOCKED позволяет запускать несколько outbox-воркеров на PostgreSQL.
    """
    with session_factory() as db, db.begin():
        rows = db.scalars(
            select(Outbox).where(Outbox.published_at.is_(None))
            .order_by(Outbox.created_at, Outbox.id).limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            event = json.loads(row.payload)
            with correlation_scope(event_correlation_id(event)):
                await bus.publish_event(row.routing_key, event)
                logger.info("outbox_event_published", extra=event_log_fields(event))
            row.published_at = utcnow()
        return len(rows)
