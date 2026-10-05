"""Integration tests for /admin/tenants/{id}/budget endpoints.

M4.D Task 4 — admin budget config + usage snapshot.

Coverage:

* POST /admin/tenants/{id}/budget creates a row with the supplied
  soft_warn_tokens / hard_cap_tokens / period_anchor_tz.
* POST same tenant twice upserts (no 409, no error).
* GET /admin/tenants/{id}/budget reads back the row.
* GET /admin/tenants/{id}/budget/usage returns ``tokens_used=0``
  initially (no ``llm_usage`` rows for a fresh tenant).

Auth (M4.C follow-up, M4.D does not regress):

* All endpoints require ``Authorization: Bearer <admin JWT>``.
* Cross-tenant access returns 404 (anti-enumeration), never 403 — matches
  M4.C kb-drafts + llm-configs pattern.
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
async def test_post_creates_budget_row(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """POST creates a budget row that GET can read back."""
    tenant = await TenantRepository().create(name="Admin Budget Create", plan=TenantPlan.PRO)
    try:
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/budget",
            json={
                "soft_warn_tokens": 800,
                "hard_cap_tokens": 1000,
                "period_anchor_tz": "UTC",
            },
            headers=auth_headers(token),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["soft_warn_tokens"] == 800
        assert body["hard_cap_tokens"] == 1000
        assert body["period_anchor_tz"] == "UTC"
        assert "updated_at" in body

        # GET reads it back.
        get_resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/budget",
            headers=auth_headers(token),
        )
        assert get_resp.status_code == 200, get_resp.text
        got = get_resp.json()
        assert got["soft_warn_tokens"] == 800
        assert got["hard_cap_tokens"] == 1000
        assert got["period_anchor_tz"] == "UTC"
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_upserts_existing_row(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """POST same tenant twice updates the row (no 409 / no error)."""
    tenant = await TenantRepository().create(name="Admin Budget Upsert", plan=TenantPlan.PRO)
    try:
        token = admin_token_for(tenant_id=tenant.id)
        first = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/budget",
            json={
                "soft_warn_tokens": 800,
                "hard_cap_tokens": 1000,
            },
            headers=auth_headers(token),
        )
        assert first.status_code == 201, first.text
        second = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/budget",
            json={
                "soft_warn_tokens": 1600,
                "hard_cap_tokens": 2000,
            },
            headers=auth_headers(token),
        )
        assert second.status_code == 201, second.text
        # upsert overwrites the previous values.
        assert second.json()["soft_warn_tokens"] == 1600
        assert second.json()["hard_cap_tokens"] == 2000
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_returns_current_budget(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """GET reads back what was POSTed (round-trip)."""
    tenant = await TenantRepository().create(name="Admin Budget Get", plan=TenantPlan.PRO)
    try:
        token = admin_token_for(tenant_id=tenant.id)
        await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/budget",
            json={
                "soft_warn_tokens": 500,
                "hard_cap_tokens": 750,
                "period_anchor_tz": "UTC",
            },
            headers=auth_headers(token),
        )
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/budget",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["soft_warn_tokens"] == 500
        assert body["hard_cap_tokens"] == 750
        assert body["period_anchor_tz"] == "UTC"
        assert "updated_at" in body
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_usage_returns_period_and_tokens_used(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """GET usage returns the expected fields; ``tokens_used=0`` initially."""
    tenant = await TenantRepository().create(name="Admin Budget Usage", plan=TenantPlan.PRO)
    try:
        token = admin_token_for(tenant_id=tenant.id)
        # POST a budget first so the usage endpoint has something to bind to.
        await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/budget",
            json={
                "soft_warn_tokens": 800,
                "hard_cap_tokens": 1000,
            },
            headers=auth_headers(token),
        )
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/budget/usage",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Required fields from spec §5.5.
        assert "period" in body
        assert body["period"]  # non-empty "YYYY-MM"
        assert body["tokens_used"] == 0  # no llm_usage rows for a fresh tenant
        assert body["soft_warn_tokens"] == 800
        assert body["hard_cap_tokens"] == 1000
        assert "period_starts_at" in body
    finally:
        await _delete_tenant(tenant.id)


# ---------------------------------------------------------------------------
# Auth — mirrors M4.C (tech debt follow-up).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_without_token_returns_401(async_client: AsyncClient) -> None:
    """No Authorization header → 401 (require_admin fires before repo)."""
    tenant = await TenantRepository().create(name="Admin Budget Auth", plan=TenantPlan.PRO)
    try:
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/budget",
            json={"soft_warn_tokens": 800, "hard_cap_tokens": 1000},
        )
        assert resp.status_code == 401
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_with_cross_tenant_admin_returns_404(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """Admin token for tenant A posting to tenant B's path → 404 (anti-enumeration)."""
    tenant_a = await TenantRepository().create(name="Admin Budget Cross A", plan=TenantPlan.PRO)
    tenant_b = await TenantRepository().create(name="Admin Budget Cross B", plan=TenantPlan.PRO)
    try:
        token = admin_token_for(tenant_id=tenant_a.id)
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant_b.id}/budget",
            json={"soft_warn_tokens": 800, "hard_cap_tokens": 1000},
            headers=auth_headers(token),
        )
        assert resp.status_code == 404
        # Anti-enumeration: detail must not distinguish "exists, wrong tenant"
        # from "doesn't exist".
        assert "not found" in resp.json().get("detail", "").lower()
    finally:
        await _delete_tenant(tenant_a.id)
        await _delete_tenant(tenant_b.id)


__all__ = [
    "test_post_creates_budget_row",
    "test_post_upserts_existing_row",
    "test_get_returns_current_budget",
    "test_get_usage_returns_period_and_tokens_used",
    "test_post_without_token_returns_401",
    "test_post_with_cross_tenant_admin_returns_404",
]
