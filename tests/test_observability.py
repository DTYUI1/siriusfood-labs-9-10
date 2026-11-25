from uuid import UUID

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from app.main import app
from event_bus.observability import current_correlation_id


def test_correlation_is_generated_or_echoed_for_http_responses(client):
    received = client.get("/missing", headers={"X-Correlation-ID": "defense-demo42"})
    generated = client.get("/another-missing", headers={"X-Correlation-ID": "bad input"})
    assert received.status_code == generated.status_code == 404
    assert received.headers["X-Correlation-ID"] == "defense-demo42"
    assert UUID(generated.headers["X-Correlation-ID"]).version == 4
    assert current_correlation_id() is None


def test_metrics_use_route_templates_instead_of_user_ids_paths_or_queries(client):
    client.post("/orders/8675309/pay", params={"private-query": "unused"})
    client.get("/arbitrary-user-text")
    text = client.get("/metrics").text
    assert "8675309" not in text
    assert "private-query" not in text
    assert "arbitrary-user-text" not in text
    metrics = {metric.name: metric.samples for metric in text_string_to_metric_families(text)}
    assert any(
        sample.labels == {"method": "POST", "route": "/orders/{order_id}/pay", "status": "401"}
        for sample in metrics["sirius_http_requests"]
    )


def test_uncaught_failure_echoes_correlation_and_counts_as_500(client, monkeypatch):
    from app.modules.menu.repository import MenuRepository

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(MenuRepository, "get_all", fail)
    with TestClient(app, raise_server_exceptions=False) as failing_client:
        response = failing_client.get("/menu/items", headers={"X-Correlation-ID": "failed-request"})
    assert response.status_code == 500
    assert response.headers["X-Correlation-ID"] == "failed-request"
    assert 'route="/menu/items",status="500"' in client.get("/metrics").text
