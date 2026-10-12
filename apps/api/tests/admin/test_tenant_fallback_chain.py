"""Integration tests for /admin/tenants/{id}/fallback-chain endpoints.

M5.1 Task 3 — admin per-tenant LLM fallback chain endpoints.

Coverage:

* POST /admin/tenants/{id}/fallback-chain creates/updates the chain
  and invalidates the per-process cache.
* GET /admin/tenants/{id}/fallback-chain returns the chain row;
  404 when none exists.
* DELETE /admin/tenants/{id}/fallback-chain removes the row +
  invalidates the cache; idempotent.
* Cross-tenant access returns 404 (anti-enumeration).
* Non-admin tokens get 403; missing tokens get 401.
* All callers go through the :class:`AdminTenantFallbackChainRepository`
  wrapper (anti-enumeration tenant-existence check).
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

import agent.llm_factory
from llm_client.tenant_fallback_chain_models import TenantFallbackChain
from tenant.enums import TenantPlan
from tenant.repository import TenantRepository

# DRY: ``_delete_tenant`` + ``auth_headers`` come from the suite conftest.
from tests.admin.conftest import _delete_tenant, auth_headers


def _fake_chain_row(tenant_id: str) -> TenantFallbackChain:
    """Return an in-memory TenantFallbackChain for cache priming in tests.

    The fields set here are the only ones the cache code path reads in
    the cache-invalidation tests — ``tenant_id`` + ``steps`` + ``enabled``.
    """
    return TenantFallbackChain(
        id="dummy",
        tenant_id=tenant_id,
        steps=[
            {
                "provider": "anthropic",
                "model": "claude-3-5-sonnet-20241022",
                "enabled": True,
            },
        ],
        enabled=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_creates_chain_200(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """POST with 2 steps returns 200 + full response shape."""
    tenant = await TenantRepository().create(
        name="Admin Chain Create", plan=TenantPlan.PRO,
    )
    try:
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/fallback-chain",
            json={
                "steps": [
                    {"provider": "anthropic", "model": "claude-3-5-sonnet-20241022"},
                    {"provider": "openai", "model": "gpt-4o"},
                ],
                "enabled": True,
            },
            headers=auth_headers(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == tenant.id
        assert body["enabled"] is True
        assert len(body["steps"]) == 2
        assert body["steps"][0]["provider"] == "anthropic"
        assert body["steps"][0]["model"] == "claude-3-5-sonnet-20241022"
        assert body["steps"][0]["enabled"] is True
        assert body["steps"][1]["provider"] == "openai"
        assert body["steps"][1]["model"] == "gpt-4o"
        assert "created_at" in body
        assert "updated_at" in body
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_updates_existing_chain_200(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """POST same tenant twice updates ``updated_at`` (upsert)."""
    tenant = await TenantRepository().create(
        name="Admin Chain Update", plan=TenantPlan.PRO,
    )
    try:
        token = admin_token_for(tenant_id=tenant.id)
        first = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/fallback-chain",
            json={
                "steps": [{"provider": "anthropic", "model": "claude-3-5-sonnet-20241022"}],
            },
            headers=auth_headers(token),
        )
        assert first.status_code == 200, first.text
        second = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/fallback-chain",
            json={
                "steps": [{"provider": "openai", "model": "gpt-4o"}],
            },
            headers=auth_headers(token),
        )
        assert second.status_code == 200, second.text
        assert second.json()["updated_at"] != first.json()["updated_at"]
        assert second.json()["steps"][0]["provider"] == "openai"
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_validates_at_least_one_enabled_step_422(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """All steps enabled=False → 422 with the validation message."""
    tenant = await TenantRepository().create(
        name="Admin Chain NoEnabled", plan=TenantPlan.PRO,
    )
    try:
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/fallback-chain",
            json={
                "steps": [
                    {
                        "provider": "anthropic",
                        "model": "claude-3-5-sonnet-20241022",
                        "enabled": False,
                    },
                ],
            },
            headers=auth_headers(token),
        )
        assert resp.status_code == 422, resp.text
        assert "chain must contain at least one enabled step" in resp.text
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_validates_unknown_provider_422(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """provider="bogus" → 422 (Pydantic Literal)."""
    tenant = await TenantRepository().create(
        name="Admin Chain BadProvider", plan=TenantPlan.PRO,
    )
    try:
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/fallback-chain",
            json={
                "steps": [{"provider": "bogus", "model": "x"}],
            },
            headers=auth_headers(token),
        )
        assert resp.status_code == 422, resp.text
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_validates_empty_model_422(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """model="" → 422 (Pydantic min_length=1)."""
    tenant = await TenantRepository().create(
        name="Admin Chain EmptyModel", plan=TenantPlan.PRO,
    )
    try:
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/fallback-chain",
            json={
                "steps": [{"provider": "anthropic", "model": ""}],
            },
            headers=auth_headers(token),
        )
        assert resp.status_code == 422, resp.text
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_requires_admin_403(
    async_client: AsyncClient,
    non_admin_token_for,
) -> None:
    """Agent role → 403 (require_admin blocks)."""
    tenant = await TenantRepository().create(
        name="Admin Chain NonAdmin", plan=TenantPlan.PRO,
    )
    try:
        token = non_admin_token_for(tenant_id=tenant.id)
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/fallback-chain",
            json={
                "steps": [{"provider": "anthropic", "model": "claude-3-5-sonnet-20241022"}],
            },
            headers=auth_headers(token),
        )
        assert resp.status_code == 403
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_no_auth_401(async_client: AsyncClient) -> None:
    """No Authorization header → 401 (require_admin fires)."""
    tenant = await TenantRepository().create(
        name="Admin Chain NoAuth", plan=TenantPlan.PRO,
    )
    try:
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/fallback-chain",
            json={
                "steps": [{"provider": "anthropic", "model": "claude-3-5-sonnet-20241022"}],
            },
        )
        assert resp.status_code == 401
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_cross_tenant_returns_404(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """Admin of tenant A POSTing to tenant B → 404 (anti-enumeration)."""
    tenant_a = await TenantRepository().create(
        name="Admin Chain Cross A", plan=TenantPlan.PRO,
    )
    tenant_b = await TenantRepository().create(
        name="Admin Chain Cross B", plan=TenantPlan.PRO,
    )
    try:
        token = admin_token_for(tenant_id=tenant_a.id)
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant_b.id}/fallback-chain",
            json={
                "steps": [{"provider": "anthropic", "model": "claude-3-5-sonnet-20241022"}],
            },
            headers=auth_headers(token),
        )
        assert resp.status_code == 404
        assert "not found" in resp.json().get("detail", "").lower()
    finally:
        await _delete_tenant(tenant_a.id)
        await _delete_tenant(tenant_b.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_returns_chain_200(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """Seed via direct repo upsert, then GET returns 200 with the same steps."""
    from llm_client.tenant_fallback_chain_models import TenantFallbackChainRepository

    tenant = await TenantRepository().create(
        name="Admin Chain Get", plan=TenantPlan.PRO,
    )
    try:
        # Reset fallback chain cache singleton so the GET path sees the
        # freshly-seeded row (not a negative-cached one from a previous test).
        agent.llm_factory._fallback_chain_cache = None
        agent.llm_factory._tenant_cache = None
        # Seed directly.
        seeded = await TenantFallbackChainRepository().upsert(
            tenant_id=tenant.id,
            steps=[
                {"provider": "anthropic", "model": "claude-3-5-sonnet-20241022", "enabled": True},
            ],
            enabled=True,
        )
        assert seeded is not None
        # Bust again so the GET below hits the DB rather than a stale cache.
        agent.llm_factory._fallback_chain_cache = None
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/fallback-chain",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == tenant.id
        assert body["enabled"] is True
        assert body["steps"][0]["provider"] == "anthropic"
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_returns_404_when_no_chain(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """GET on a tenant with no chain row → 404."""
    tenant = await TenantRepository().create(
        name="Admin Chain GetEmpty", plan=TenantPlan.PRO,
    )
    try:
        # Reset cache so the negative-lookup doesn't serve a stale "None".
        agent.llm_factory._fallback_chain_cache = None
        agent.llm_factory._tenant_cache = None
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/fallback-chain",
            headers=auth_headers(token),
        )
        assert resp.status_code == 404
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_cross_tenant_returns_404(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """Cross-tenant GET → 404 (anti-enumeration)."""
    tenant_a = await TenantRepository().create(
        name="Admin Chain GetCross A", plan=TenantPlan.PRO,
    )
    tenant_b = await TenantRepository().create(
        name="Admin Chain GetCross B", plan=TenantPlan.PRO,
    )
    try:
        token = admin_token_for(tenant_id=tenant_a.id)
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant_b.id}/fallback-chain",
            headers=auth_headers(token),
        )
        assert resp.status_code == 404
        assert "not found" in resp.json().get("detail", "").lower()
    finally:
        await _delete_tenant(tenant_a.id)
        await _delete_tenant(tenant_b.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_returns_204(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """Seed then DELETE → 204; subsequent GET → 404."""
    from llm_client.tenant_fallback_chain_models import TenantFallbackChainRepository

    tenant = await TenantRepository().create(
        name="Admin Chain Delete", plan=TenantPlan.PRO,
    )
    try:
        # Reset cache so the GET-after-delete hits the DB.
        agent.llm_factory._fallback_chain_cache = None
        agent.llm_factory._tenant_cache = None
        await TenantFallbackChainRepository().upsert(
            tenant_id=tenant.id,
            steps=[
                {"provider": "anthropic", "model": "claude-3-5-sonnet-20241022", "enabled": True},
            ],
            enabled=True,
        )
        # Bust cache before DELETE so the DELETE-busts-cache test below
        # gets a fresh singleton.
        agent.llm_factory._fallback_chain_cache = None
        agent.llm_factory._tenant_cache = None
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.delete(
            f"/api/v1/admin/tenants/{tenant.id}/fallback-chain",
            headers=auth_headers(token),
        )
        assert resp.status_code == 204, resp.text
        # Subsequent GET should be 404.
        # Bust cache again so it doesn't serve a stale positive entry.
        agent.llm_factory._fallback_chain_cache = None
        agent.llm_factory._tenant_cache = None
        get_resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/fallback-chain",
            headers=auth_headers(token),
        )
        assert get_resp.status_code == 404
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_idempotent_returns_204(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """DELETE no-row → still 204 (idempotent)."""
    tenant = await TenantRepository().create(
        name="Admin Chain DeleteIdempotent", plan=TenantPlan.PRO,
    )
    try:
        agent.llm_factory._fallback_chain_cache = None
        agent.llm_factory._tenant_cache = None
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.delete(
            f"/api/v1/admin/tenants/{tenant.id}/fallback-chain",
            headers=auth_headers(token),
        )
        assert resp.status_code == 204, resp.text
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_cross_tenant_returns_404(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """Cross-tenant DELETE → 404 (anti-enumeration)."""
    tenant_a = await TenantRepository().create(
        name="Admin Chain DeleteCross A", plan=TenantPlan.PRO,
    )
    tenant_b = await TenantRepository().create(
        name="Admin Chain DeleteCross B", plan=TenantPlan.PRO,
    )
    try:
        token = admin_token_for(tenant_id=tenant_a.id)
        resp = await async_client.delete(
            f"/api/v1/admin/tenants/{tenant_b.id}/fallback-chain",
            headers=auth_headers(token),
        )
        assert resp.status_code == 404
        assert "not found" in resp.json().get("detail", "").lower()
    finally:
        await _delete_tenant(tenant_a.id)
        await _delete_tenant(tenant_b.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_invalidates_cache(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """Prime cache → POST → cache invalidated."""
    tenant = await TenantRepository().create(
        name="Admin Chain InvalidatePost", plan=TenantPlan.PRO,
    )
    try:
        # Reset to a fresh singleton for this test.
        agent.llm_factory._fallback_chain_cache = None
        agent.llm_factory._tenant_cache = None
        cache = agent.llm_factory._build_fallback_chain_cache()
        # Prime the cache with a fake row.
        fake_row = _fake_chain_row(tenant.id)
        cache.put(tenant.id, fake_row)
        assert cache.get(tenant.id) is fake_row

        # POST a real chain via the API.
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.post(
            f"/api/v1/admin/tenants/{tenant.id}/fallback-chain",
            json={
                "steps": [{"provider": "openai", "model": "gpt-4o"}],
            },
            headers=auth_headers(token),
        )
        assert resp.status_code == 200, resp.text

        # Cache must be invalidated (not the fake row).
        assert cache.get(tenant.id) is None
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_invalidates_cache(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """Prime cache → DELETE → cache invalidated."""
    from llm_client.tenant_fallback_chain_models import TenantFallbackChainRepository

    tenant = await TenantRepository().create(
        name="Admin Chain InvalidateDelete", plan=TenantPlan.PRO,
    )
    try:
        # Reset + seed a real row.
        agent.llm_factory._fallback_chain_cache = None
        agent.llm_factory._tenant_cache = None
        await TenantFallbackChainRepository().upsert(
            tenant_id=tenant.id,
            steps=[
                {"provider": "anthropic", "model": "claude-3-5-sonnet-20241022", "enabled": True},
            ],
            enabled=True,
        )
        # Reset again so the cache is empty before priming.
        agent.llm_factory._fallback_chain_cache = None
        agent.llm_factory._tenant_cache = None
        cache = agent.llm_factory._build_fallback_chain_cache()
        # Prime with the fake row (real row is in DB; we just want to
        # verify the DELETE invalidates the cache).
        fake_row = _fake_chain_row(tenant.id)
        cache.put(tenant.id, fake_row)
        assert cache.get(tenant.id) is fake_row

        # DELETE via API.
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.delete(
            f"/api/v1/admin/tenants/{tenant.id}/fallback-chain",
            headers=auth_headers(token),
        )
        assert resp.status_code == 204, resp.text

        # Cache invalidated.
        assert cache.get(tenant.id) is None
    finally:
        await _delete_tenant(tenant.id)


__all__ = [
    "test_delete_cross_tenant_returns_404",
    "test_delete_idempotent_returns_204",
    "test_delete_invalidates_cache",
    "test_delete_returns_204",
    "test_get_cross_tenant_returns_404",
    "test_get_returns_404_when_no_chain",
    "test_get_returns_chain_200",
    "test_post_creates_chain_200",
    "test_post_cross_tenant_returns_404",
    "test_post_invalidates_cache",
    "test_post_no_auth_401",
    "test_post_requires_admin_403",
    "test_post_updates_existing_chain_200",
    "test_post_validates_at_least_one_enabled_step_422",
    "test_post_validates_empty_model_422",
    "test_post_validates_unknown_provider_422",
]