"""Integration tests for admin tenant widget config endpoints.

Tier 2 Task 2.4 — Widget config CRUD.

Three endpoints are tested:

* ``GET    /api/v1/admin/tenants/{tid}/widget-config`` — read
* ``PATCH  /api/v1/admin/tenants/{tid}/widget-config`` — upsert
* ``DELETE /api/v1/admin/tenants/{tid}/widget-config`` — reset (idempotent)

When the row is absent (tenant has never customized), GET materializes
the M2.A defaults in memory without writing to the DB. The first PATCH
materializes the row.

Multi-tenant isolation
----------------------

* Cross-tenant access returns 404 (anti-enumeration, same as the rest
  of the admin surface).
* Non-admin tokens get 403.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tenant.models import Tenant
from tests.admin.conftest import auth_headers


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_widget_config_returns_defaults_when_absent(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """GET on a freshly-seeded tenant returns the M2.A defaults."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/widget-config",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == sample_tenant.id
    assert body["theme"] == "light"
    assert body["primary_color"] == "#2563eb"
    assert body["position"] == "bottom-right"
    assert body["widget_title"] == "在线客服"
    assert body["enabled"] is True
    assert body["logo_url"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_widget_config_creates_then_updates(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """First PATCH materializes the row; second PATCH preserves omitted fields."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    # First PATCH: theme + primary color only.
    first = await async_client.patch(
        f"/api/v1/admin/tenants/{sample_tenant.id}/widget-config",
        json={"theme": "dark", "primary_color": "#ff00aa"},
        headers=auth_headers(token),
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["theme"] == "dark"
    assert body["primary_color"] == "#ff00aa"
    # Untouched fields preserved.
    assert body["position"] == "bottom-right"
    assert body["widget_title"] == "在线客服"
    assert body["enabled"] is True

    # Second PATCH: position only — earlier fields preserved.
    second = await async_client.patch(
        f"/api/v1/admin/tenants/{sample_tenant.id}/widget-config",
        json={"position": "bottom-left"},
        headers=auth_headers(token),
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["theme"] == "dark"
    assert body["primary_color"] == "#ff00aa"
    assert body["position"] == "bottom-left"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_widget_config_rejects_bad_color(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """PATCH with a non-hex color returns 422 (Pydantic pattern check)."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.patch(
        f"/api/v1/admin/tenants/{sample_tenant.id}/widget-config",
        json={"primary_color": "not-a-color"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_widget_config_rejects_invalid_theme(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """PATCH with a non-allowlisted theme returns 422."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.patch(
        f"/api/v1/admin/tenants/{sample_tenant.id}/widget-config",
        json={"theme": "neon"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_widget_config_is_idempotent(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """DELETE resets to defaults; second DELETE returns the same response."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    # First create the row.
    await async_client.patch(
        f"/api/v1/admin/tenants/{sample_tenant.id}/widget-config",
        json={"theme": "dark"},
        headers=auth_headers(token),
    )
    # First DELETE.
    first = await async_client.delete(
        f"/api/v1/admin/tenants/{sample_tenant.id}/widget-config",
        headers=auth_headers(token),
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["reset"] is True
    assert body["tenant_id"] == sample_tenant.id
    # Second DELETE — idempotent.
    again = await async_client.delete(
        f"/api/v1/admin/tenants/{sample_tenant.id}/widget-config",
        headers=auth_headers(token),
    )
    assert again.status_code == 200, again.text
    # Subsequent GET returns the defaults again.
    after = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/widget-config",
        headers=auth_headers(token),
    )
    assert after.status_code == 200, after.text
    assert after.json()["theme"] == "light"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_widget_config_anti_enumeration_cross_tenant_404(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Admin of a different tenant gets 404 (anti-enumeration)."""
    other_tenant_id = "01HZOTHER00000000000000000"
    token = admin_token_for(tenant_id=other_tenant_id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/widget-config",
        headers=auth_headers(token),
    )
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_widget_config_requires_admin_role(
    async_client: AsyncClient, sample_tenant: Tenant, non_admin_token_for
) -> None:
    """Non-admin (agent) token gets 403."""
    token = non_admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/widget-config",
        headers=auth_headers(token),
    )
    assert resp.status_code == 403