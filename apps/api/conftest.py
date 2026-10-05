import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "src"))


def _postgres_reachable() -> bool:
    """Best-effort check for a live Postgres backend (testcontainer or local).

    Used to auto-skip ``skip_postgres``-marked tests in CI environments
    without a PG backend. The check is intentionally cached at conftest
    import time to avoid hitting the DB once per test.
    """
    try:
        from sqlalchemy import text
        from core.config import get_settings
        from core.database import get_engine

        settings = get_settings()
        if not settings.database_url:
            return False
        engine = get_engine()

        async def _ping() -> bool:
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                return True
            except Exception:
                return False

        import asyncio
        return asyncio.run(_ping())
    except Exception:
        return False


_PG_AVAILABLE = _postgres_reachable()


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip ``skip_postgres``-marked tests when no PG backend is reachable.

    The marker is registered in ``pyproject.toml`` so ``--strict-markers``
    is satisfied; this hook adds the actual skip behavior. Heavier M4.*
    PG coverage lives in test_repository.py + integration tests — the
    smoke tests only verify schema registration (no DB needed).
    """
    if _PG_AVAILABLE:
        return
    for item in items:
        if "skip_postgres" in item.keywords:
            item.add_marker(
                pytest.mark.skip(reason="no Postgres testcontainer available")
            )


@pytest.fixture(autouse=True)
def _reset_singletons() -> None:
    """Reset all module-level singletons before each test for isolation.

    Each pytest-asyncio test gets its own event loop. Without this, the
    SQLAlchemy engine and Redis client from a previous test would be
    reused on a closed loop, raising 'Event loop is closed'.
    """
    from core.database import reset_engine, reset_sessionmaker
    from core.qdrant import reset_qdrant_client
    from core.redis import reset_redis

    reset_engine()
    reset_sessionmaker()
    reset_redis()
    reset_qdrant_client()
    yield
    # post-test cleanup happens naturally when the event loop closes