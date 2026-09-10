import asyncio
from typing import Any

from sqlalchemy import text

from core.database import get_session
from core.qdrant import get_qdrant_client
from core.redis import get_redis


async def check_postgres() -> dict[str, Any]:
    try:
        async with get_session() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def check_redis() -> dict[str, Any]:
    try:
        r = get_redis()
        await r.ping()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def check_qdrant() -> dict[str, Any]:
    try:
        client = get_qdrant_client()
        await client.get_collections()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def aggregate_health() -> dict[str, Any]:
    pg, rd, qd = await asyncio.gather(
        check_postgres(), check_redis(), check_qdrant()
    )
    components = {
        "postgres": pg["status"],
        "redis": rd["status"],
        "qdrant": qd["status"],
    }
    all_ok = all(c == "ok" for c in components.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "components": components,
    }
