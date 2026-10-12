"""Integration tests for the admin assignment policy endpoints.

Two endpoints:

* ``GET    /api/v1/admin/tenants/{tid}/assignment-policy``
* ``PATCH  /api/v1/admin/tenants/{tid}/assignment-policy``

When the policy is absent (tenant has never configured it), GET
materialises the opt-out default (``enabled=False,
strategy='manual'``). The first PATCH materialises the row by
writing the full sub-dict into ``tenants.settings['assignment_policy']``.

Multi-tenant isolation
----------------------

* Cross-tenant access returns 404 (anti-enumeration, same as the
  rest of the admin surface).
* Non-admin tokens get 403 (covered by ``require_admin``).
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tenant.models import Tenant
from tenant.repository import TenantRepository
from tests.admin.conftest import auth_headers


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_assignment_policy_returns_defaults_when_absent(
    async_client: AsyncClient,
    sample_tenant: Tenant,
    admin_token_for,
) -> None:
    """Fresh tenant → materialised default response (no DB write)."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/assignment-policy",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"enabled": False, "strategy": "manual"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_assignment_policy_creates_then_updates(
    async_client: AsyncClient,
    sample_tenant: Tenant,
    admin_token_for,
) -> None:
    """First PATCH materialises; second PATCH preserves omitted fields."""
    token = admin_token_for(tenant_id=sample_tenant.id)

    # First PATCH: only enabled.
    first = await async_client.patch(
        f"/api/v1/admin/tenants/{sample_tenant.id}/assignment-policy",
        json={"enabled": True},
        headers=auth_headers(token),
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["enabled"] is True
    assert body["strategy"] == "manual"  # default preserved

    # Second PATCH: only strategy — enabled stays True.
    second = await async_client.patch(
        f"/api/v1/admin/tenants/{sample_tenant.id}/assignment-policy",
        json={"strategy": "least_busy"},
        headers=auth_headers(token),
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["enabled"] is True
    assert body["strategy"] == "least_busy"

    # GET reflects the merged state.
    third = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/assignment-policy",
        headers=auth_headers(token),
    )
    assert third.json() == {"enabled": True, "strategy": "least_busy"}

    # And the underlying storage matches.
    repo = TenantRepository()
    stored = await repo.get_assignment_policy(sample_tenant.id)
    assert stored == {"enabled": True, "strategy": "least_busy"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_assignment_policy_rejects_unknown_strategy(
    async_client: AsyncClient,
    sample_tenant: Tenant,
    admin_token_for,
) -> None:
    """Unknown strategy value → 422 from Pydantic before handler runs."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.patch(
        f"/api/v1/admin/tenants/{sample_tenant.id}/assignment-policy",
        json={"strategy": "round_robin"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 422, resp.text
    # And the policy was NOT persisted.
    stored = await TenantRepository().get_assignment_policy(sample_tenant.id)
    assert stored is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_assignment_policy_anti_enumeration_cross_tenant_404(
    async_client: AsyncClient,
    sample_tenant: Tenant,
    admin_token_for,
) -> None:
    """An admin token for a different tenant gets 404, not 403.

    Anti-enumeration: the response must not differentiate between
    "tenant doesn't exist" and "tenant exists but you don't own it".
    """
    other_tenant_id = "01HZOTHER00000000000000000"
    token = admin_token_for(tenant_id=other_tenant_id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/assignment-policy",
        headers=auth_headers(token),
    )
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_assignment_policy_anti_enumeration_cross_tenant_404(
    async_client: AsyncClient,
    sample_tenant: Tenant,
    admin_token_for,
) -> None:
    """Cross-tenant PATCH also 404s, and the policy is NOT written."""
    other_tenant_id = "01HZOTHER00000000000000000"
    token = admin_token_for(tenant_id=other_tenant_id)
    resp = await async_client.patch(
        f"/api/v1/admin/tenants/{sample_tenant.id}/assignment-policy",
        json={"enabled": True, "strategy": "least_busy"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 404
    # Storage on the target tenant must be untouched.
    stored = await TenantRepository().get_assignment_policy(sample_tenant.id)
    assert stored is None
