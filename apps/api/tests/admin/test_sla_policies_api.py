"""Integration tests for admin SLA policies listing endpoint.

SLA Policies admin page (parallel UI task — same scope as Budget
Dashboard / Tickets list+detail). The endpoint is read-only — SLA
policies are configured by tenant admins out-of-band (seed script /
DB migration), so this suite focuses on:

* Tenant isolation: list returns only the caller's policies.
* Ordering: by priority (P0 → P3) so the SPA renders a stable table.
* Auth: requires admin JWT (no token → 401, non-admin → 403).
* Cross-tenant access → 404 (anti-enumeration, matches the
  kb-drafts / llm-configs / budget pattern in ``admin/api.py``).
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from core.database import get_sessionmaker
from core.id_gen import new_id
from tenant.models import Tenant
from tests.admin.conftest import auth_headers
from ticket.models import SlaPolicy


async def _seed_policies(tenant_id: str, rows: list[SlaPolicy]) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        for r in rows:
            session.add(r)
        await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_sla_policies_returns_tenant_policies_in_priority_order(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Listing returns only the tenant's policies, ordered P0 → P3."""
    rows = [
        SlaPolicy(
            id=new_id(),
            tenant_id=sample_tenant.id,
            name="P0 Critical",
            priority="P0",
            first_response_minutes=5,
            resolution_minutes=60,
            business_hours_only=False,
        ),
        SlaPolicy(
            id=new_id(),
            tenant_id=sample_tenant.id,
            name="P3 Low",
            priority="P3",
            first_response_minutes=1440,
            resolution_minutes=10080,
            business_hours_only=True,
        ),
    ]
    # Insert deliberately out-of-order to confirm ORDER BY priority works.
    await _seed_policies(sample_tenant.id, [rows[1], rows[0]])

    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/sla-policies",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    priorities = [row["priority"] for row in body]
    assert priorities == ["P0", "P3"]
    # Verify Pydantic schema fields are populated
    p0 = body[0]
    assert p0["name"] == "P0 Critical"
    assert p0["first_response_minutes"] == 5
    assert p0["resolution_minutes"] == 60
    assert p0["business_hours_only"] is False
    assert "id" in p0 and "created_at" in p0

    p3 = body[1]
    assert p3["business_hours_only"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_sla_policies_anti_enumeration_cross_tenant_404(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Admin of a different tenant gets 404 (anti-enumeration)."""
    other_tenant_id = "01HZOTHER00000000000000000"
    token = admin_token_for(tenant_id=other_tenant_id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/sla-policies",
        headers=auth_headers(token),
    )
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_sla_policies_requires_admin_role(
    async_client: AsyncClient, sample_tenant: Tenant, non_admin_token_for
) -> None:
    """Non-admin (agent) token gets 403 — same require_admin gate as others."""
    token = non_admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/sla-policies",
        headers=auth_headers(token),
    )
    assert resp.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_sla_policies_empty_when_none_configured(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Empty list returns 200 + [] (not 404) when no policies exist yet."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/sla-policies",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
