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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_usage_unknown_tenant_returns_404(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """Anti-enumeration: unknown tenant_id on /usage must return 404, not 500.

    Regression for Task 4 review — without the try/except ValueError,
    AdminTenantBudgetRepository.get() raised ValueError which FastAPI
    translated to 500, breaking anti-enumeration (could distinguish
    'tenant exists but cross-tenant' from 'tenant does not exist').

    Both branches — cross-tenant (hardcoded ``"not found"`` detail) and
    unknown-tenant (ValueError ``"unknown tenant: ..."`` detail) — must
    agree on status code 404. The detail text differs by branch but
    status_code uniformity is the load-bearing invariant.
    """
    fake_tenant_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"  # valid ULID format
    token = admin_token_for(tenant_id=fake_tenant_id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{fake_tenant_id}/budget/usage",
        headers=auth_headers(token),
    )
    # Load-bearing invariant: 404 (not 500). Same status code as the
    # cross-tenant branch — anti-enumeration holds.
    assert resp.status_code == 404
    assert resp.status_code != 500
    # The repository's ValueError becomes the detail. Either the
    # hardcoded "not found" (cross-tenant) or the repository's
    # "unknown tenant: ..." string is fine — both keep status 404.
    detail = resp.json().get("detail", "").lower()
    assert ("not found" in detail) or ("unknown tenant" in detail)


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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cleanup_endpoint_requires_jwt_auth(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """POST /admin/budget/cleanup — JWT auth gate.

    - Without bearer token → 401
    - With regular admin JWT (claims has tenant_id) → 404 (anti-enumeration)
    - With super-admin JWT (claims has no tenant_id claim) → 200 with stats

    The super-admin token is issued by calling ``create_access_token`` with
    ``extra={"tenant_id": None}`` — the JWT layer accepts this and the
    resulting claims dict has ``claims.get("tenant_id")`` return ``None``,
    which is what the anti-enumeration check bypasses on.
    """
    from auth.jwt import create_access_token

    # 401 — no token
    resp = await async_client.post("/api/v1/admin/budget/cleanup")
    assert resp.status_code == 401

    # 404 — admin token (has tenant_id claim)
    tenant_token = admin_token_for(tenant_id="t-someone-else", user_id="admin-1")
    resp = await async_client.post(
        "/api/v1/admin/budget/cleanup",
        headers=auth_headers(tenant_token),
    )
    assert resp.status_code == 404

    # 200 — super-admin (no tenant_id claim via extra override).
    # The role must still satisfy ``require_admin``'s gate (admin/owner),
    # so we use ``role="admin"`` here — the load-bearing part for
    # bypassing the anti-enumeration check is ``tenant_id=None``.
    super_token = create_access_token(
        tenant_id="ignored",  # required by signature; overridden below
        user_id="super-1",
        role="admin",
        extra={"tenant_id": None},  # JWT payload's tenant_id becomes None
    )
    resp = await async_client.post(
        "/api/v1/admin/budget/cleanup",
        headers=auth_headers(super_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "deleted_rows" in body
    assert "cutoff_period" in body


__all__ = [
    "test_post_creates_budget_row",
    "test_post_upserts_existing_row",
    "test_get_returns_current_budget",
    "test_get_usage_returns_period_and_tokens_used",
    "test_get_usage_unknown_tenant_returns_404",
    "test_post_without_token_returns_401",
    "test_post_with_cross_tenant_admin_returns_404",
    "test_cleanup_endpoint_requires_jwt_auth",
]
