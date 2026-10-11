"""Integration tests for admin tenant user (agent/admin) CRUD endpoints.

Tier 2 Task 2.2 — Agent/User management.

Covers the five endpoints:

* ``GET    /api/v1/admin/tenants/{tenant_id}/users`` — list
* ``POST   /api/v1/admin/tenants/{tenant_id}/users`` — create (bcrypt-hash pw)
* ``PATCH  /api/v1/admin/tenants/{tenant_id}/users/{user_id}`` — role / full_name / is_active
* ``POST   /api/v1/admin/tenants/{tenant_id}/users/{user_id}/reset-password``
* ``DELETE /api/v1/admin/tenants/{tenant_id}/users/{user_id}`` — idempotent

Multi-tenant isolation
----------------------

* Cross-tenant access returns 404 (anti-enumeration, same pattern as
  kb-drafts / llm-configs / budget / sla-policies).
* Non-admin tokens get 403.

PII discipline
--------------

``email`` + ``full_name`` are PII; tests use opaque identifiers like
``alice@example.com`` so logs do not leak real customer data.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tenant.enums import UserRole
from tenant.models import Tenant, User
from tests.admin.conftest import auth_headers


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_users_empty_when_none_configured(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """GET returns [] (not 404) when the tenant has no users yet."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/users",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_users_excludes_password_hash(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """GET must never expose ``password_hash`` (PII / secret material)."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    # Create one user via POST.
    create = await async_client.post(
        f"/api/v1/admin/tenants/{sample_tenant.id}/users",
        json={
            "email": "alice@example.com",
            "password": "supersecret123",
            "full_name": "Alice Operator",
            "role": "agent",
        },
        headers=auth_headers(token),
    )
    assert create.status_code == 201, create.text
    listing = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/users",
        headers=auth_headers(token),
    )
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert len(body) == 1
    user = body[0]
    assert user["email"] == "alice@example.com"
    assert user["full_name"] == "Alice Operator"
    assert user["role"] == "agent"
    assert user["is_active"] is True
    assert "password_hash" not in user
    assert "password" not in user


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_users_anti_enumeration_cross_tenant_404(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Admin of a different tenant gets 404 (anti-enumeration)."""
    other_tenant_id = "01HZOTHER00000000000000000"
    token = admin_token_for(tenant_id=other_tenant_id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/users",
        headers=auth_headers(token),
    )
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_users_requires_admin_role(
    async_client: AsyncClient, sample_tenant: Tenant, non_admin_token_for
) -> None:
    """Non-admin (agent) token gets 403."""
    token = non_admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/users",
        headers=auth_headers(token),
    )
    assert resp.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_creates_user_with_hashed_password(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """POST creates a user with bcrypt-hashed password (never plaintext)."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.post(
        f"/api/v1/admin/tenants/{sample_tenant.id}/users",
        json={
            "email": "bob@example.com",
            "password": "supersecret123",
            "full_name": "Bob Builder",
            "role": "admin",
        },
        headers=auth_headers(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == "bob@example.com"
    assert body["role"] == "admin"
    assert body["is_active"] is True

    # Verify the password_hash is bcrypt, not plaintext.
    sm = __import__("core.database", fromlist=["get_sessionmaker"]).get_sessionmaker()
    async with sm() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(User).where(
                User.tenant_id == sample_tenant.id,
                User.email == "bob@example.com",
            )
        )
        row = result.scalar_one()
        # bcrypt hashes start with $2a$, $2b$ or $2y$.
        assert row.password_hash.startswith("$2"), "password_hash should be bcrypt"
        assert "supersecret123" not in row.password_hash


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_duplicate_email_returns_409(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """POST duplicate (tenant_id, email) returns 409."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    payload = {
        "email": "carol@example.com",
        "password": "supersecret123",
        "role": "agent",
    }
    first = await async_client.post(
        f"/api/v1/admin/tenants/{sample_tenant.id}/users",
        json=payload,
        headers=auth_headers(token),
    )
    assert first.status_code == 201, first.text
    again = await async_client.post(
        f"/api/v1/admin/tenants/{sample_tenant.id}/users",
        json=payload,
        headers=auth_headers(token),
    )
    assert again.status_code == 409, again.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_then_reset_password_then_delete_user(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """PATCH / reset-password / DELETE — happy-path CRUD smoke."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    created = await async_client.post(
        f"/api/v1/admin/tenants/{sample_tenant.id}/users",
        json={
            "email": "dave@example.com",
            "password": "original123",
            "role": "agent",
        },
        headers=auth_headers(token),
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]

    # PATCH: promote to admin + set full_name + disable.
    patched = await async_client.patch(
        f"/api/v1/admin/tenants/{sample_tenant.id}/users/{user_id}",
        json={"role": "admin", "full_name": "Dave Newname", "is_active": False},
        headers=auth_headers(token),
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["role"] == "admin"
    assert body["full_name"] == "Dave Newname"
    assert body["is_active"] is False

    # Reset password.
    reset = await async_client.post(
        f"/api/v1/admin/tenants/{sample_tenant.id}/users/{user_id}/reset-password",
        json={"new_password": "newsecret456"},
        headers=auth_headers(token),
    )
    assert reset.status_code == 204, reset.text

    # DELETE the user.
    deleted = await async_client.delete(
        f"/api/v1/admin/tenants/{sample_tenant.id}/users/{user_id}",
        headers=auth_headers(token),
    )
    assert deleted.status_code == 204, deleted.text
    # Re-DELETE is idempotent.
    again = await async_client.delete(
        f"/api/v1/admin/tenants/{sample_tenant.id}/users/{user_id}",
        headers=auth_headers(token),
    )
    assert again.status_code == 204, again.text
    # GET shows empty list.
    listing = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/users",
        headers=auth_headers(token),
    )
    assert listing.status_code == 200, listing.text
    assert listing.json() == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_self_returns_400(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """DELETE self is rejected with 400 (so admins can't lock themselves out)."""
    token = admin_token_for(
        tenant_id=sample_tenant.id, user_id="01HZSELF000000000000000000"
    )
    resp = await async_client.delete(
        f"/api/v1/admin/tenants/{sample_tenant.id}/users/01HZSELF000000000000000000",
        headers=auth_headers(token),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_user_from_other_tenant_returns_404(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """PATCH for a user belonging to a different tenant returns 404
    (anti-enumeration, same as the listing endpoint)."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    other_user_id = "01HZOTHER000000000000USER00"
    resp = await async_client.patch(
        f"/api/v1/admin/tenants/{sample_tenant.id}/users/{other_user_id}",
        json={"role": "admin"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 404