"""Live-DB fixtures for admin KB drafts API tests.

Stage 18 / M2.B Task 8.

Mirrors the per-suite pattern from
``tests/channel/integration/conftest.py`` (singleton reset +
httpx ``AsyncClient`` wired to the FULL FastAPI app + tenant factory).

The ``admin/api.py`` module mounts under ``/api/v1/admin`` on the
main app — we deliberately wire the FULL app (rather than a
sliced admin-only FastAPI) so the routes are tested in the same code
path as the real deployment.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from core.database import get_session, get_sessionmaker, reset_engine, reset_sessionmaker
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
async def async_client() -> AsyncIterator[AsyncClient]:
    """Yield an httpx ``AsyncClient`` wired to the FULL FastAPI app."""
    from main import app  # noqa: PLC0415 — import-after-fixture for fast collection

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
async def db_session() -> AsyncIterator[Any]:
    """Yield the application sessionmaker."""
    sm = get_sessionmaker()
    async with sm() as session:
        yield session


async def _delete_tenant(tenant_id: str) -> None:
    """Cascade-delete a tenant."""
    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t is not None:
            await session.delete(t)
            await session.commit()


@pytest.fixture
async def sample_tenant() -> AsyncIterator[Tenant]:
    """Yield a fresh Tenant. Cleanup is cascade-driven."""
    tenant = await TenantRepository().create(
        name="KB Drafts Test Tenant", plan=TenantPlan.FREE
    )
    try:
        yield tenant
    finally:
        await _delete_tenant(tenant.id)


__all__ = ["async_client", "db_session", "sample_tenant"]