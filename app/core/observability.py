"""HTTP correlation ID и RED-метрики с ограниченной кардинальностью."""
import logging
from time import perf_counter
from uuid import uuid4

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy.pool import QueuePool
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import Response

from event_bus.observability import correlation_scope, valid_correlation_id

logger = logging.getLogger(__name__)
HEADER = "x-correlation-id"
HTTP_METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS", "HEAD"}
REQUESTS = Counter("sirius_http_requests", "HTTP requests", ["method", "route", "status"])
DURATION = Histogram(
    "sirius_http_request_duration_seconds", "HTTP request duration", ["method", "route"],
)
IN_PROGRESS = Gauge("sirius_http_in_progress", "Concurrent HTTP requests")
CACHE_READS = Counter("sirius_cache_reads", "Cache lookups", ["result"])
for result in ("hit", "miss", "error"):
    CACHE_READS.labels(result)
DATABASE_CONNECTIONS = Gauge("sirius_db_pool_connections", "API database connections", ["state"])

# Первая выборка Prometheus должна увидеть нули до первого учебного заказа,
# иначе rate не покажет прирост между запуском API и первой выборкой.
for method, route in (
    ("GET", "/menu/items"),
    ("GET", "/menu/items/{item_id}"),
    ("POST", "/orders/create"),
    ("POST", "/orders/{order_id}/pay"),
):
    DURATION.labels(method, route)
    for status in ("200", "500"):
        REQUESTS.labels(method, route, status)


class ObservabilityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/metrics":
            await self.app(scope, receive, send)
            return

        correlation_id = valid_correlation_id(Headers(scope=scope).get(HEADER)) or str(uuid4())
        scope.setdefault("state", {})["correlation_id"] = correlation_id
        method = scope["method"] if scope["method"] in HTTP_METHODS else "OTHER"
        status = 500

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[HEADER] = correlation_id
            await send(message)

        with correlation_scope(correlation_id), IN_PROGRESS.track_inprogress():
            started = perf_counter()
            try:
                await self.app(scope, receive, send_with_id)
            finally:
                elapsed = perf_counter() - started
                # Пользовательский путь и query string не попадают ни в labels, ни в лог.
                route = getattr(scope.get("route"), "path", None) or "unmatched"
                REQUESTS.labels(method, route, str(status)).inc()
                DURATION.labels(method, route).observe(elapsed)
                logger.info(
                    "http_request", extra={
                        "method": method, "route": route, "status_code": status,
                        "duration_ms": round(elapsed * 1000, 2),
                    },
                )


def metrics_response(pool):
    if isinstance(pool, QueuePool):
        DATABASE_CONNECTIONS.labels("in_use").set(pool.checkedout())
        DATABASE_CONNECTIONS.labels("pool_size").set(pool.size())
    return Response(generate_latest(), headers={"content-type": CONTENT_TYPE_LATEST})
