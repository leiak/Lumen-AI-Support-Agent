from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_metrics_endpoint_exposes_prometheus_format() -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "# HELP http_requests_total" in body
    assert "# HELP http_request_duration_seconds" in body
    assert "http_requests_total" in body


def test_metrics_records_requests_and_normalizes_paths() -> None:
    # Hit a dynamic path (ULID-like) to check cardinality collapsing.
    client.get("/api/v1/conversations/01ARZ3ZZEKXZJP6RQENPP5ZFE9/messages")
    body = client.get("/metrics").text
    # The ULID segment must be collapsed to a stable /:id label.
    assert 'method="GET",path="/api/v1/conversations/:id/messages"' in body
    assert "/01ARZ3ZZEKXZJP6RQENPP5ZFE9/messages" not in body
