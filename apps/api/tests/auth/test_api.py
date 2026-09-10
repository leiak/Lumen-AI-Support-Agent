"""Integration tests for the auth login API. Requires live DB."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from auth.password import hash_password
from core.database import get_session
from core.id_gen import new_id
from main import app
from tenant.enums import TenantPlan, TenantStatus, UserRole
from tenant.models import Tenant
from tenant.repository import UserRepository


@pytest.fixture
def seeded_tenant_user():
    """Seed a tenant + user; returns (tenant_id, email, password)."""
    from core.database import reset_engine, reset_sessionmaker

    async def _seed():
        tenant_id = new_id()
        async with get_session() as session:
            t = Tenant(
                id=tenant_id,
                name=f"Acme-{tenant_id[:8]}",
                plan=TenantPlan.PRO,
                status=TenantStatus.ACTIVE,
            )
            session.add(t)
            await session.flush()
            await session.commit()
        user = await UserRepository().create(
            tenant_id=tenant_id,
            email=f"alice-{tenant_id}@acme.com",
            password_hash=hash_password("password123"),
            role=UserRole.ADMIN,
            full_name="Alice",
        )
        return tenant_id, user.email, "password123"

    result = asyncio.run(_seed())
    # The seed used its own event loop; the engine bound to that loop is now
    # unusable. Clear it so the TestClient (which runs on a fresh loop)
    # creates a new one.
    reset_engine()
    reset_sessionmaker()
    return result


def test_login_success(seeded_tenant_user) -> None:
    tenant_id, email, password = seeded_tenant_user
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
        headers={"X-Tenant-Id": tenant_id},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"  # noqa: S105
    assert body["access_token"]
    assert body["user"]["email"] == email
    assert body["user"]["tenant_id"] == tenant_id
    assert body["user"]["role"] == "admin"


def test_login_wrong_password(seeded_tenant_user) -> None:
    tenant_id, email, _ = seeded_tenant_user
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "wrong-password"},
        headers={"X-Tenant-Id": tenant_id},
    )
    assert resp.status_code == 401


def test_login_missing_tenant_header(seeded_tenant_user) -> None:
    _, email, password = seeded_tenant_user
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
        # NO X-Tenant-Id header
    )
    assert resp.status_code == 422  # FastAPI's required-header validation
