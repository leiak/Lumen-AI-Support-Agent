from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_health_endpoint_returns_ok_when_dependencies_up() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["components"]["postgres"] == "ok"
    assert data["components"]["redis"] == "ok"
    assert data["components"]["qdrant"] == "ok"
