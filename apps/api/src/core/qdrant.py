from qdrant_client import AsyncQdrantClient

from core.config import get_settings

_client: AsyncQdrantClient | None = None


def get_qdrant_client() -> AsyncQdrantClient:
    global _client
    if _client is None:
        settings = get_settings()
        _client = AsyncQdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
            # REST only — docker-compose exposes 6333, not the gRPC port 6334.
            # Keep prefer_grpc=False so future maintainers don't "optimize" this back.
            prefer_grpc=False,
        )
    return _client


async def close_qdrant_client() -> None:
    """Close the Qdrant client. For shutdown."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def reset_qdrant_client() -> None:
    """Clear the cached Qdrant client. For test isolation only.
    Note: does NOT close the connection — the next caller will get a fresh client."""
    global _client
    _client = None