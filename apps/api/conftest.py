import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "src"))


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