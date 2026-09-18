import asyncio
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from core.config import get_settings
from core.database import get_session
from core.qdrant import get_qdrant_client
from core.redis import get_redis

# Module-level startup timestamp. Set lazily on the first call to
# :func:`liveness` so it tracks "process first became responsive",
# not "module first imported" (which fires during ``from main import app``
# inside tests, where the lifespan never runs). ``None`` means "not yet
# seen a request" — only relevant in pure unit-test contexts where /health
# is never hit.
_STARTED_AT: datetime | None = None


def _app_info() -> dict[str, str]:
    """Build the static ``version`` / ``started_at`` block.

    Centralized so ``liveness`` and ``readiness`` agree on the shape.
    """
    settings = get_settings()
    global _STARTED_AT
    if _STARTED_AT is None:
        _STARTED_AT = datetime.now(UTC)
    return {
        "service": settings.service_name,
        "version": settings.service_version,
        "git_sha": settings.git_sha or "unknown",
        "started_at": _STARTED_AT.isoformat(),
    }


def liveness() -> dict[str, Any]:
    """Process-alive check. Returns immediately, performs **no** IO.

    Suitable as a Kubernetes ``livenessProbe`` — failing this means the
    process itself is wedged (event loop stuck, GC death spiral). It
    deliberately does not touch Postgres / Redis / Qdrant because a
    transient dependency outage should NOT cause Kubernetes to restart
    the pod (that just thrashes the cluster).

    Side effect: lazy-initializes ``_STARTED_AT`` so the very first
    /health/live after process boot records the wall-clock start.
    """
    body: dict[str, Any] = {"status": "alive"}
    body.update(_app_info())
    return body


def _sanitize_error(e: Exception) -> str:
    """Return a safe error string. In production, do not leak driver details."""
    settings = get_settings()
    if settings.environment.value == "production":
        return "unavailable"
    return f"{type(e).__name__}: {e}"


async def check_postgres() -> dict[str, str]:
    try:
        async with get_session() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": _sanitize_error(e)}


async def check_redis() -> dict[str, str]:
    try:
        r = get_redis()
        await r.ping()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": _sanitize_error(e)}


async def check_qdrant() -> dict[str, str]:
    try:
        client = get_qdrant_client()
        await client.get_collections()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": _sanitize_error(e)}


async def aggregate_health() -> tuple[dict[str, Any], bool]:
    """Run all component checks in parallel. Returns (body, all_ok)."""
    pg, rd, qd = await asyncio.gather(
        check_postgres(), check_redis(), check_qdrant()
    )
    components: dict[str, str] = {
        "postgres": pg["status"],
        "redis": rd["status"],
        "qdrant": qd["status"],
    }
    all_ok = all(c == "ok" for c in components.values())
    body: dict[str, Any] = {
        "status": "ok" if all_ok else "degraded",
        "components": components,
    }
    return body, all_ok


async def readiness() -> tuple[dict[str, Any], bool]:
    """Dependency-ready check. 503 on degraded; suitable for k8s readinessProbe.

    Wraps :func:`aggregate_health` and decorates the body with the
    same ``version`` / ``started_at`` block that ``/health/live``
    carries, so a single ``jq .version`` works regardless of which
    probe the operator is inspecting.
    """
    body, all_ok = await aggregate_health()
    # Stage 11.4: include build metadata so the on-call engineer can
    # see at a glance whether they're hitting the right image. ``_app_info``
    # lazily sets ``_STARTED_AT`` on first call.
    for key, value in _app_info().items():
        body[key] = value
    return body, all_ok