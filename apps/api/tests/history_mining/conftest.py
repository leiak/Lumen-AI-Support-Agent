"""Shared fixtures for history_mining integration tests.

Stage 18 / M2.B Task 8.

Mirrors the per-suite pattern from
``tests/channel/integration/conftest.py``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from core.database import get_session, reset_engine, reset_sessionmaker
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository


@pytest.fixture(autouse=True)
def _reset_db_singletons() -> None:
    """Reset engine / sessionmaker so each test gets a fresh event loop."""
    reset_engine()
    reset_sessionmaker()
    yield
    reset_engine()
    reset_sessionmaker()


@pytest.fixture
async def sample_tenant() -> AsyncIterator[Tenant]:
    """Yield a fresh Tenant. Cleanup is cascade-driven."""
    tenant = await TenantRepository().create(
        name="History Mining Test Tenant", plan=TenantPlan.FREE
    )

    async def _delete() -> None:
        async with get_session() as session:
            t = await session.get(Tenant, tenant.id)
            if t is not None:
                await session.delete(t)
                await session.commit()

    try:
        yield tenant
    finally:
        await _delete()


@pytest.fixture
async def db_session() -> AsyncIterator[Any]:
    """Yield the application sessionmaker for direct row assertions."""
    from core.database import get_sessionmaker

    sm = get_sessionmaker()
    async with sm() as session:
        yield session


__all__ = ["sample_tenant", "db_session"]