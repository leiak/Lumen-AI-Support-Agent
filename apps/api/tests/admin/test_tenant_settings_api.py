"""Integration tests for admin tenant self-settings endpoints.

Tier 2 Task 2.3 — Tenant management (tenant-scoped).

Two endpoints are tested:

* ``GET   /api/v1/admin/tenants/{tenant_id}/settings``
* ``PATCH /api/v1/admin/tenants/{tenant_id}/settings``

Scope: a tenant-scoped admin can view + edit their own tenant's
``name`` / ``plan`` / ``status``. Cross-tenant access returns 404
(anti-enumeration, same as the rest of the admin surface). Listing
all tenants would require a super-admin role that does NOT exist in
the current JWT model — that's deliberately out of scope.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tenant.enums import TenantPlan, TenantStatus
from tenant.models import Tenant
from tests.admin.conftest import auth_headers


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_tenant_settings_returns_tenant_state(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """GET returns the tenant's current name/plan/status."""
    # Update plan via the repository directly so we can verify it surfaces.
    from tenant.repository import TenantRepository

    await TenantRepository().update_profile(
        sample_tenant.id, name="Renamed Tenant", plan=TenantPlan.PRO
    )

    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/settings",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == sample_tenant.id
    assert body["name"] == "Renamed Tenant"
    assert body["plan"] == "pro"
    assert body["status"] == "active"
    assert "created_at" in body
    assert "updated_at" in body


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_tenant_settings_anti_enumeration_cross_tenant_404(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Admin of a different tenant gets 404 (anti-enumeration)."""
    other_tenant_id = "01HZOTHER00000000000000000"
    token = admin_token_for(tenant_id=other_tenant_id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/settings",
        headers=auth_headers(token),
    )
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_tenant_settings_requires_admin_role(
    async_client: AsyncClient, sample_tenant: Tenant, non_admin_token_for
) -> None:
    """Non-admin (agent) token gets 403."""
    token = non_admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/settings",
        headers=auth_headers(token),
    )
    assert resp.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_tenant_settings_updates_name_plan_status(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """PATCH updates all three mutable fields and returns the refreshed row."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.patch(
        f"/api/v1/admin/tenants/{sample_tenant.id}/settings",
        json={"name": "Acme Corp", "plan": "enterprise", "status": "suspended"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Acme Corp"
    assert body["plan"] == "enterprise"
    assert body["status"] == "suspended"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_tenant_settings_preserves_omitted_fields(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """PATCH with a partial payload preserves omitted fields."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    # First set a known baseline.
    baseline = await async_client.patch(
        f"/api/v1/admin/tenants/{sample_tenant.id}/settings",
        json={"name": "Baseline", "plan": "pro"},
        headers=auth_headers(token),
    )
    assert baseline.status_code == 200, baseline.text
    # Then update only the status — name/plan should be preserved.
    resp = await async_client.patch(
        f"/api/v1/admin/tenants/{sample_tenant.id}/settings",
        json={"status": "suspended"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Baseline"
    assert body["plan"] == "pro"
    assert body["status"] == "suspended"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_tenant_settings_rejects_deleted_status(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """PATCH status=deleted returns 400 (super-admin only, not exposed)."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.patch(
        f"/api/v1/admin/tenants/{sample_tenant.id}/settings",
        json={"status": "deleted"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_tenant_settings_rejects_empty_name(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """PATCH with an empty name returns 422 (Pydantic Field constraint)."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.patch(
        f"/api/v1/admin/tenants/{sample_tenant.id}/settings",
        json={"name": ""},
        headers=auth_headers(token),
    )
    assert resp.status_code == 422, resp.text