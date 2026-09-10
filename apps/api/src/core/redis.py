import redis.asyncio as aioredis

from core.config import get_settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        settings = get_settings()
        _client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def reset_redis() -> None:
    """Clear the cached redis client. For test isolation only.
    Note: does NOT close the connection — the next caller will get a fresh client."""
    global _client
    _client = None
