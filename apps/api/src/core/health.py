import asyncio
from typing import Any

from sqlalchemy import text

from core.config import get_settings
from core.database import get_session
from core.qdrant import get_qdrant_client
from core.redis import get_redis


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