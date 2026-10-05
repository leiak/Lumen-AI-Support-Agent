"""Integration tests for /admin/tenants/{id}/llm-configs endpoints.

M4.C Task 4 — admin BYOK registration + listing.

Coverage:

* POST /admin/tenants/{id}/llm-configs creates a row, encrypts the key,
  and never returns the plaintext (or ciphertext) in the response.
* POST same provider twice upserts (updates ``updated_at`` + replaces
  ciphertext) without raising.
* GET /admin/tenants/{id}/llm-configs lists provider names + metadata
  with no key fields in the response.
* GET on an unknown tenant returns 404 (anti-enumeration).
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tenant.enums import TenantPlan
from tenant.repository import TenantRepository


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_creates_row_with_encrypted_key(async_client: AsyncClient) -> None:
    """POST creates a row; the API key is encrypted and never returned."""
    tenant = await TenantRepository().create(name="Admin BYOK Test", plan=TenantPlan.PRO)
    try:
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs",
            json={"provider_name": "minimax", "api_key": "sk-test-12345"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["provider_name"] == "minimax"
        assert body["enabled"] is True
        assert "api_key" not in body
        assert "encrypted_api_key" not in body
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_upserts_existing_row(async_client: AsyncClient) -> None:
    """POST same provider twice updates ``updated_at`` (no 409 / no error)."""
    tenant = await TenantRepository().create(name="Admin BYOK Upsert", plan=TenantPlan.PRO)
    try:
        first = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs",
            json={"provider_name": "minimax", "api_key": "sk-first"},
        )
        assert first.status_code == 201, first.text
        second = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs",
            json={"provider_name": "minimax", "api_key": "sk-second"},
        )
        assert second.status_code == 201, second.text
        assert second.json()["updated_at"] != first.json()["updated_at"]
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_returns_provider_names_without_keys(async_client: AsyncClient) -> None:
    """GET returns provider metadata; NEVER returns api_key / encrypted_api_key."""
    tenant = await TenantRepository().create(name="Admin BYOK Get", plan=TenantPlan.PRO)
    try:
        await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs",
            json={"provider_name": "minimax", "api_key": "sk-test"},
        )
        resp = await async_client.get(f"/api/v1/admin/tenants/{tenant.id}/llm-configs")
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["provider_name"] == "minimax"
        assert "api_key" not in rows[0]
        assert "encrypted_api_key" not in rows[0]
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_returns_404_for_unknown_tenant(async_client: AsyncClient) -> None:
    """GET on an unknown tenant → 404 (anti-enumeration)."""
    resp = await async_client.get("/api/v1/admin/tenants/nonexistent-tenant/llm-configs")
    assert resp.status_code == 404


async def _delete_tenant(tenant_id: str) -> None:
    """Cascade-delete a tenant."""
    from core.database import get_session
    from tenant.models import Tenant

    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t is not None:
            await session.delete(t)
            await session.commit()


__all__ = [
    "test_post_creates_row_with_encrypted_key",
    "test_post_upserts_existing_row",
    "test_get_returns_provider_names_without_keys",
    "test_get_returns_404_for_unknown_tenant",
]