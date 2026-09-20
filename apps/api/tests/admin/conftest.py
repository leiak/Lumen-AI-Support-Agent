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

from auth.jwt import create_access_token
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


def auth_headers(token: str) -> dict[str, str]:
    """Build the ``Authorization`` header dict for a JWT bearer token."""
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_token_for():
    """Return a callable that issues an admin JWT for the given tenant.

    Usage in a test::

        async def test_x(async_client, sample_tenant, admin_token_for):
            token = admin_token_for(tenant_id=sample_tenant.id, user_id="admin-1")
            resp = await async_client.get("/api/v1/admin/kb-drafts",
                                           headers=auth_headers(token))
    """

    def _make(*, tenant_id: str, user_id: str = "admin-1") -> str:
        return create_access_token(
            tenant_id=tenant_id, user_id=user_id, role="admin"
        )

    return _make


@pytest.fixture
def non_admin_token_for():
    """Return a callable that issues a non-admin (agent) JWT for the given tenant."""

    def _make(*, tenant_id: str, user_id: str = "agent-1") -> str:
        return create_access_token(
            tenant_id=tenant_id, user_id=user_id, role="agent"
        )

    return _make


__all__ = [
    "admin_token_for",
    "async_client",
    "auth_headers",
    "db_session",
    "non_admin_token_for",
    "sample_tenant",
]