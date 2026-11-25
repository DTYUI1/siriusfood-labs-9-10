"""Контекст и формат логов, общие для HTTP и участников событий."""
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

_correlation_id = ContextVar("correlation_id", default=None)
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}")
_LOG_FIELDS = ("order_id", "event_id", "event_type", "method", "route", "status_code", "duration_ms")


def valid_correlation_id(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def current_correlation_id():
    return _correlation_id.get()


@contextmanager
def correlation_scope(correlation_id):
    token = _correlation_id.set(correlation_id)
    try:
        yield
    finally:
        _correlation_id.reset(token)


def event_correlation_id(payload):
    # Outbox, созданный до добавления поля, можно прочитать после обновления.
    return valid_correlation_id(payload.get("correlation_id")) or payload["event_id"]


def event_log_fields(payload):
    return {key: payload[key] for key in ("order_id", "event_id", "event_type")}


class JsonFormatter(logging.Formatter):
    def __init__(self, service):
        super().__init__()
        self.service = service

    def format(self, record):
        data = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        correlation_id = current_correlation_id()
        if correlation_id:
            data["correlation_id"] = correlation_id
        data.update({field: getattr(record, field) for field in _LOG_FIELDS if hasattr(record, field)})
        return json.dumps(data, ensure_ascii=False)


def configure_json_logging(service, *, uvicorn=False):
    formatter = JsonFormatter(service)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handlers = [handler]
    directory = os.getenv("LAB_TRACE_DIR")
    if directory and service in {"api", "kitchen", "outbox", "delivery"}:
        demo_log = RotatingFileHandler(Path(directory) / (service+".jsonl"), maxBytes=512*1024,
                                       backupCount=1, encoding="utf-8", delay=True)
        messages = {"order_created", "order_paid_outbox_committed", "outbox_event_published",
                    "kitchen_event_processed", "delivery_event_processed"}
        demo_log.addFilter(lambda record: (current_correlation_id() or "").startswith("demo-")
                           and record.msg in messages)
        demo_log.setFormatter(formatter)
        handlers.append(demo_log)
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    if uvicorn:
        for name in ("uvicorn", "uvicorn.access"):
            logger = logging.getLogger(name)
            if logger.handlers:
                logger.handlers = [handler]
