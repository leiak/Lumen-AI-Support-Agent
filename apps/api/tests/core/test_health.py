"""Stage 11.4: /health endpoint split + version + started_at.

The pre-Stage-11.4 single ``/health`` endpoint is split into:

* ``GET /health/live`` — process alive, **no** IO. 200 unconditionally.
  Suitable for Kubernetes ``livenessProbe``. A dependency outage must
  not cause this to fail (that just restarts pods and amplifies the
  outage).
* ``GET /health/ready`` — dependency readiness. 503 when any of
  Postgres / Redis / Qdrant is degraded. Suitable for ``readinessProbe``
  and load-balancer health checks.
* ``GET /health`` — kept as a back-compat alias for ``/health/ready``
  plus the ``version`` / ``started_at`` / ``git_sha`` block, so old
  operator dashboards keep working.

All three responses include ``version`` and ``started_at`` so a single
``jq .version`` works regardless of which probe the on-call engineer
is inspecting. ``_STARTED_AT`` is recorded lazily on the first call to
``/health/live`` so unit tests (where the FastAPI lifespan never runs)
still get a sensible value.
"""
from __future__ import annotations

import re
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


_ISO_8601_UTC = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)$"
)


def test_health_live_always_200() -> None:
    """``/health/live`` returns 200 even when ALL deps are down.

    This is the contract that lets Kubernetes avoid thrashing pods
    during a transient DB outage.
    """
    with patch("core.health.check_postgres", side_effect=RuntimeError("pg dead")), \
         patch("core.health.check_redis", side_effect=RuntimeError("redis dead")), \
         patch("core.health.check_qdrant", side_effect=RuntimeError("qd dead")):
        response = client.get("/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "alive"
    assert body["service"] == "ai-customer-api"
    assert "version" in body
    assert "started_at" in body


def test_health_live_does_not_touch_dependencies() -> None:
    """Liveness must perform zero DB / Redis / Qdrant IO."""
    with patch("core.health.check_postgres") as pg, \
         patch("core.health.check_redis") as rd, \
         patch("core.health.check_qdrant") as qd:
        response = client.get("/health/live")
    assert response.status_code == 200
    pg.assert_not_called()
    rd.assert_not_called()
    qd.assert_not_called()


def test_health_ready_returns_503_when_dependencies_down() -> None:
    """When any dep is degraded, ``/health/ready`` returns 503.

    The production ``check_*`` helpers swallow internal exceptions and
    return ``{"status": "error", ...}`` — so we mock them at the same
    surface (return value), not by raising from the mock itself.
    """
    with patch("core.health.check_postgres", return_value={"status": "error", "error": "pg dead"}), \
         patch("core.health.check_redis", return_value={"status": "error", "error": "redis dead"}), \
         patch("core.health.check_qdrant", return_value={"status": "error", "error": "qd dead"}):
        response = client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["components"]["postgres"] != "ok"
    assert body["components"]["redis"] != "ok"
    assert body["components"]["qdrant"] != "ok"


def test_health_includes_version_and_started_at() -> None:
    """All three endpoints surface ``version`` + ``started_at``."""
    for path in ("/health/live", "/health/ready", "/health"):
        response = client.get(path)
        body = response.json()
        assert "version" in body, f"{path} missing version"
        assert "started_at" in body, f"{path} missing started_at"
        # ``started_at`` should parse as an ISO-8601 UTC timestamp.
        assert _ISO_8601_UTC.match(body["started_at"]), (
            f"{path} started_at not ISO-8601 UTC: {body['started_at']!r}"
        )


def test_health_alias_returns_same_body_as_ready() -> None:
    """``/health`` is a back-compat alias for ``/health/ready``.

    Same status code, same payload (modulo the version/started_at block
    which is identical on both endpoints because they share
    :func:`core.health.readiness`).
    """
    live = client.get("/health/ready").json()
    alias = client.get("/health").json()
    assert live == alias
