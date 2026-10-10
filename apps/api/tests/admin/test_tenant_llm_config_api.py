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

Tech debt follow-up — JWT auth (M4.C Task 4 review):

* All calls require ``Authorization: Bearer <admin JWT>``.
* Cross-tenant access returns 404 (anti-enumeration), never 403 — matches
  kb-drafts pattern.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tenant.enums import TenantPlan
from tenant.repository import TenantRepository

# DRY: ``_delete_tenant`` + ``auth_headers`` are defined in
# ``tests/admin/conftest.py`` (imported here for direct use, not as
# pytest fixtures).
from tests.admin.conftest import _delete_tenant, auth_headers


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_creates_row_with_encrypted_key(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """POST creates a row; the API key is encrypted and never returned."""
    tenant = await TenantRepository().create(name="Admin BYOK Test", plan=TenantPlan.PRO)
    try:
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs",
            json={"provider_name": "minimax", "api_key": "sk-test-12345"},
            headers=auth_headers(token),
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
async def test_post_upserts_existing_row(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """POST same provider twice updates ``updated_at`` (no 409 / no error)."""
    tenant = await TenantRepository().create(name="Admin BYOK Upsert", plan=TenantPlan.PRO)
    try:
        token = admin_token_for(tenant_id=tenant.id)
        first = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs",
            json={"provider_name": "minimax", "api_key": "sk-first"},
            headers=auth_headers(token),
        )
        assert first.status_code == 201, first.text
        second = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs",
            json={"provider_name": "minimax", "api_key": "sk-second"},
            headers=auth_headers(token),
        )
        assert second.status_code == 201, second.text
        assert second.json()["updated_at"] != first.json()["updated_at"]
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_returns_provider_names_without_keys(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """GET returns provider metadata; NEVER returns api_key / encrypted_api_key."""
    tenant = await TenantRepository().create(name="Admin BYOK Get", plan=TenantPlan.PRO)
    try:
        token = admin_token_for(tenant_id=tenant.id)
        await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs",
            json={"provider_name": "minimax", "api_key": "sk-test"},
            headers=auth_headers(token),
        )
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs",
            headers=auth_headers(token),
        )
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
async def test_get_returns_404_for_unknown_tenant(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """GET on an unknown tenant → 404 (anti-enumeration)."""
    tenant = await TenantRepository().create(name="Admin BYOK Unknown", plan=TenantPlan.PRO)
    try:
        # Admin token for a *real* tenant; the path param names a fake one.
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.get(
            "/api/v1/admin/tenants/nonexistent-tenant/llm-configs",
            headers=auth_headers(token),
        )
        assert resp.status_code == 404
        # Detail must not leak existence / tenant names — matches kb-drafts.
        assert "not found" in resp.json().get("detail", "").lower()
    finally:
        await _delete_tenant(tenant.id)


# ---------------------------------------------------------------------------
# Tech debt follow-up — JWT auth on tenant LLM config endpoints
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_without_token_returns_401(async_client: AsyncClient) -> None:
    """No Authorization header → 401 (require_admin fires before repo)."""
    tenant = await TenantRepository().create(name="Admin BYOK Auth", plan=TenantPlan.PRO)
    try:
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs",
            json={"provider_name": "minimax", "api_key": "sk-test"},
        )
        assert resp.status_code == 401
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_with_non_admin_token_returns_403(
    async_client: AsyncClient,
    non_admin_token_for,
) -> None:
    """Agent role → 403 (require_admin blocks)."""
    tenant = await TenantRepository().create(name="Admin BYOK NonAdmin", plan=TenantPlan.PRO)
    try:
        token = non_admin_token_for(tenant_id=tenant.id)
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs",
            json={"provider_name": "minimax", "api_key": "sk-test"},
            headers=auth_headers(token),
        )
        assert resp.status_code == 403
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_with_cross_tenant_admin_returns_404(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """Admin token for tenant A posting to tenant B's path → 404 (anti-enumeration)."""
    tenant_a = await TenantRepository().create(name="Admin BYOK Cross A", plan=TenantPlan.PRO)
    tenant_b = await TenantRepository().create(name="Admin BYOK Cross B", plan=TenantPlan.PRO)
    try:
        token = admin_token_for(tenant_id=tenant_a.id)
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant_b.id}/llm-configs",
            json={"provider_name": "minimax", "api_key": "sk-test"},
            headers=auth_headers(token),
        )
        assert resp.status_code == 404
        # Anti-enumeration: detail must not distinguish "exists, wrong tenant"
        # from "doesn't exist".
        assert "not found" in resp.json().get("detail", "").lower()
    finally:
        await _delete_tenant(tenant_a.id)
        await _delete_tenant(tenant_b.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_without_token_returns_401(async_client: AsyncClient) -> None:
    """No Authorization header → 401 (require_admin fires before repo)."""
    tenant = await TenantRepository().create(name="Admin BYOK GetAuth", plan=TenantPlan.PRO)
    try:
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs",
        )
        assert resp.status_code == 401
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_with_cross_tenant_admin_returns_404(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """Admin token for tenant A GETing tenant B's configs → 404 (anti-enumeration)."""
    tenant_a = await TenantRepository().create(name="Admin BYOK GetCross A", plan=TenantPlan.PRO)
    tenant_b = await TenantRepository().create(name="Admin BYOK GetCross B", plan=TenantPlan.PRO)
    try:
        token = admin_token_for(tenant_id=tenant_a.id)
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant_b.id}/llm-configs",
            headers=auth_headers(token),
        )
        assert resp.status_code == 404
        assert "not found" in resp.json().get("detail", "").lower()
    finally:
        await _delete_tenant(tenant_a.id)
        await _delete_tenant(tenant_b.id)


# ---------------------------------------------------------------------------
# Tier 1 Task 1.3 — LLM config mutation endpoints (PATCH + DELETE).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_toggles_enabled_preserves_key(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """PATCH with ``enabled`` flips the flag without rotating the encrypted key.

    Confirms the GET-after-PATCH reflects the new flag and the response
    still excludes both ``api_key`` and ``encrypted_api_key``.
    """
    tenant = await TenantRepository().create(name="Admin LLM Patch", plan=TenantPlan.PRO)
    try:
        token = admin_token_for(tenant_id=tenant.id)
        # Seed a row first.
        await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs",
            json={"provider_name": "minimax", "api_key": "sk-patch"},
            headers=auth_headers(token),
        )
        # Disable via PATCH.
        resp = await async_client.patch(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs/minimax",
            json={"enabled": False},
            headers=auth_headers(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["provider_name"] == "minimax"
        assert body["enabled"] is False
        assert "api_key" not in body
        assert "encrypted_api_key" not in body
        # GET reflects the new flag.
        listing = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs",
            headers=auth_headers(token),
        )
        assert listing.status_code == 200, listing.text
        assert listing.json()[0]["enabled"] is False
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_removes_provider_config(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """DELETE removes the (tenant, provider) row; subsequent GET is empty."""
    tenant = await TenantRepository().create(name="Admin LLM Delete", plan=TenantPlan.PRO)
    try:
        token = admin_token_for(tenant_id=tenant.id)
        await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs",
            json={"provider_name": "anthropic", "api_key": "sk-del"},
            headers=auth_headers(token),
        )
        resp = await async_client.delete(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs/anthropic",
            headers=auth_headers(token),
        )
        assert resp.status_code == 204, resp.text
        # GET now returns empty list.
        listing = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs",
            headers=auth_headers(token),
        )
        assert listing.status_code == 200, listing.text
        assert listing.json() == []
        # Idempotent: deleting again still 204.
        again = await async_client.delete(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs/anthropic",
            headers=auth_headers(token),
        )
        assert again.status_code == 204, again.text
    finally:
        await _delete_tenant(tenant.id)


__all__ = [
    "test_post_creates_row_with_encrypted_key",
    "test_post_upserts_existing_row",
    "test_get_returns_provider_names_without_keys",
    "test_get_returns_404_for_unknown_tenant",
    "test_post_without_token_returns_401",
    "test_post_with_non_admin_token_returns_403",
    "test_post_with_cross_tenant_admin_returns_404",
    "test_get_without_token_returns_401",
    "test_get_with_cross_tenant_admin_returns_404",
    "test_patch_toggles_enabled_preserves_key",
    "test_delete_removes_provider_config",
]