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


# ---------------------------------------------------------------------------
# Pack B #2 — credit grant + list endpoints (super-admin only).
#
# Anti-enumeration mirrors /budget/cleanup: per-tenant admin tokens
# always carry ``tenant_id`` in claims and get a 404 on these endpoints.
# Super-admin tokens (claims have tenant_id=None via the ``extra``
# override on ``create_access_token``) bypass the gate.
# ---------------------------------------------------------------------------


def _super_admin_token() -> str:
    """Issue a super-admin JWT with ``claims['tenant_id'] == None``.

    Mirrors the pattern in ``test_cleanup_endpoint_requires_jwt_auth``
    above.
    """
    from auth.jwt import create_access_token

    return create_access_token(
        tenant_id="ignored",  # required by signature; overridden below
        user_id="super-1",
        role="admin",
        extra={"tenant_id": None},  # JWT payload's tenant_id becomes None
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_credits_as_super_admin_returns_201(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """Super-admin POST /credits returns 201 with the row echoed."""
    tenant = await TenantRepository().create(
        name="Admin Credit Grant", plan=TenantPlan.PRO
    )
    try:
        super_token = _super_admin_token()
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/credits",
            json={"tokens": 500, "note": "Q4 promo"},
            headers=auth_headers(super_token),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["tenant_id"] == tenant.id
        assert body["tokens"] == 500
        assert body["note"] == "Q4 promo"
        assert body["granted_by"] == "super-1"
        assert body["period"]  # YYYY-MM
        assert body["id"]  # ULID is non-empty
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_credits_as_per_tenant_admin_returns_404(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """Anti-enumeration: per-tenant admin probing → 404 (tenant_id != None)."""
    tenant = await TenantRepository().create(
        name="Admin Credit PerTenant", plan=TenantPlan.PRO
    )
    try:
        # Per-tenant admin JWT has tenant_id claim set to tenant.id.
        tenant_token = admin_token_for(tenant_id=tenant.id, user_id="admin-1")
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/credits",
            json={"tokens": 500, "note": "probe"},
            headers=auth_headers(tenant_token),
        )
        assert resp.status_code == 404
        # Anti-enumeration: detail must not distinguish "exists but wrong role"
        # from "doesn't exist".
        assert "not found" in resp.json().get("detail", "").lower()
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_credits_as_super_admin_returns_list(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """Super-admin GET /credits returns the credits array + total_tokens."""
    tenant = await TenantRepository().create(
        name="Admin Credit List", plan=TenantPlan.PRO
    )
    try:
        super_token = _super_admin_token()
        # Seed two grants via POST so the GET has something to return.
        for amount, note in [(100, "first"), (200, "second")]:
            r = await async_client.post(
                f"/api/v1/admin/tenants/{tenant.id}/credits",
                json={"tokens": amount, "note": note},
                headers=auth_headers(super_token),
            )
            assert r.status_code == 201, r.text

        # Resolve the current UTC period so the query matches the rows the
        # POSTs just wrote.
        from datetime import datetime, timezone

        current_period = datetime.now(timezone.utc).strftime("%Y-%m")

        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/credits?period={current_period}",
            headers=auth_headers(super_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "credits" in body
        assert "total_tokens" in body
        assert body["total_tokens"] == 300  # 100 + 200
        # Rows returned in created_at-ascending order (first, then second).
        assert len(body["credits"]) == 2
        assert body["credits"][0]["tokens"] == 100
        assert body["credits"][1]["tokens"] == 200
        assert body["credits"][0]["note"] == "first"
        assert body["credits"][1]["note"] == "second"
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_credits_as_per_tenant_admin_returns_404(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """Anti-enumeration: per-tenant admin GET /credits → 404."""
    tenant = await TenantRepository().create(
        name="Admin Credit List 404", plan=TenantPlan.PRO
    )
    try:
        tenant_token = admin_token_for(tenant_id=tenant.id, user_id="admin-1")
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/credits?period=2026-10",
            headers=auth_headers(tenant_token),
        )
        assert resp.status_code == 404
    finally:
        await _delete_tenant(tenant.id)


# ---------------------------------------------------------------------------
# Pack B #5 — snapshot endpoint `?breakdown=true` extension.
#
# Backward compatibility: omitting `?breakdown=true` returns
# `breakdown=null` (Pack A wire shape). Adding the query param
# triggers the per-model GROUP BY scan + caches it for 30s.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_usage_with_breakdown_true_includes_breakdown(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """`?breakdown=true` returns per-model array + effective_cap + credits_total."""
    tenant = await TenantRepository().create(
        name="Admin Budget Breakdown", plan=TenantPlan.PRO
    )
    try:
        token = admin_token_for(tenant_id=tenant.id)
        # POST a budget so the usage endpoint has a base_cap to bind.
        await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/budget",
            json={"soft_warn_tokens": 800, "hard_cap_tokens": 1000},
            headers=auth_headers(token),
        )
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/budget/usage?breakdown=true",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Pack B #5 new fields present.
        assert "breakdown" in body
        assert isinstance(body["breakdown"], list)
        # No llm_usage rows for this fresh tenant → empty breakdown.
        assert body["breakdown"] == []
        # Pack B #2 new fields present.
        assert "effective_cap" in body
        assert "credits_total" in body
        # No credits granted → credits_total=0; effective_cap = base (1000).
        assert body["credits_total"] == 0
        assert body["effective_cap"] == 1000
        # Pack A fields still present (no regression).
        assert "tokens_used" in body
        assert "soft_warn_tokens" in body
        assert "hard_cap_tokens" in body
        assert "period_starts_at" in body
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_usage_without_breakdown_omits_breakdown(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """Without `?breakdown=true`, `breakdown` is null (Pack A backward compat)."""
    tenant = await TenantRepository().create(
        name="Admin Budget NoBreakdown", plan=TenantPlan.PRO
    )
    try:
        token = admin_token_for(tenant_id=tenant.id)
        await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/budget",
            json={"soft_warn_tokens": 800, "hard_cap_tokens": 1000},
            headers=auth_headers(token),
        )
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/budget/usage",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Pack B #5: breakdown defaults to None when not requested.
        assert body.get("breakdown") is None
        # Pack B #2: effective_cap + credits_total are still populated
        # (they're always-on fields, not gated by `?breakdown=true`).
        assert body["effective_cap"] == 1000
        assert body["credits_total"] == 0
        # Pack A fields still present (no regression).
        assert body["tokens_used"] == 0
        assert body["soft_warn_tokens"] == 800
        assert body["hard_cap_tokens"] == 1000
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_usage_breakdown_cross_tenant_returns_404(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """Cross-tenant admin probing the usage endpoint → 404 (anti-enumeration)."""
    tenant_a = await TenantRepository().create(
        name="Admin Budget CrossBreakdown A", plan=TenantPlan.PRO
    )
    tenant_b = await TenantRepository().create(
        name="Admin Budget CrossBreakdown B", plan=TenantPlan.PRO
    )
    try:
        # Admin token for tenant A trying to read tenant B's usage.
        token_a = admin_token_for(tenant_id=tenant_a.id)
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant_b.id}/budget/usage?breakdown=true",
            headers=auth_headers(token_a),
        )
        assert resp.status_code == 404
        # Anti-enumeration: detail must not distinguish "exists, wrong
        # tenant" from "doesn't exist".
        assert "not found" in resp.json().get("detail", "").lower()
    finally:
        await _delete_tenant(tenant_a.id)
        await _delete_tenant(tenant_b.id)


__all__ = [
    "test_post_creates_budget_row",
    "test_post_upserts_existing_row",
    "test_get_returns_current_budget",
    "test_get_usage_returns_period_and_tokens_used",
    "test_get_usage_unknown_tenant_returns_404",
    "test_post_without_token_returns_401",
    "test_post_with_cross_tenant_admin_returns_404",
    "test_cleanup_endpoint_requires_jwt_auth",
    "test_post_credits_as_super_admin_returns_201",
    "test_post_credits_as_per_tenant_admin_returns_404",
    "test_get_credits_as_super_admin_returns_list",
    "test_get_credits_as_per_tenant_admin_returns_404",
    "test_get_usage_with_breakdown_true_includes_breakdown",
    "test_get_usage_without_breakdown_omits_breakdown",
    "test_get_usage_breakdown_cross_tenant_returns_404",
]
