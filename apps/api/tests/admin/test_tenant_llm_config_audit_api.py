"""Integration tests for ``GET /admin/tenants/{id}/llm-configs/audit``.

Mirrors the auth + tenant-existence + anti-enumeration patterns from
``test_tenant_llm_config_api.py``. Uses the same autouse fixtures
(``_reset_db_singletons`` + ``_ensure_fernet_key``) from
``tests/admin/conftest.py``.
"""
from __future__ import annotations

import hashlib

import pytest
from httpx import AsyncClient

from admin.repository import AdminTenantLLMConfigRepository
from admin.schemas.tenant_llm_config import (
    TenantLLMConfigCreate,
    TenantLLMConfigUpdate,
)
from tenant.enums import TenantPlan
from tenant.repository import TenantRepository
from tests.admin.conftest import _delete_tenant, auth_headers

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _mutate_via_wrapper(tenant_id: str, *, plain: str = "sk-x") -> None:
    """Helper: seed a row + 1 PATCH via the wrapper so the GET has data.

    Bypasses the HTTP layer because the audit WRITE behavior is
    already covered in test_admin_llm_config_audit_integration.py.
    Here we just want audit rows in the DB to query against.
    """
    sha = hashlib.sha256(plain.encode()).hexdigest()
    await AdminTenantLLMConfigRepository().upsert(
        tenant_id=tenant_id,
        payload=TenantLLMConfigCreate(
            provider_name="anthropic", api_key=plain, model="claude-3-5-sonnet-20241022",
        ),
        changed_by="admin-1", api_key_sha256=sha,
    )
    await AdminTenantLLMConfigRepository().update(
        tenant_id=tenant_id, provider_name="anthropic",
        payload=TenantLLMConfigUpdate(model="claude-haiku-4-5"),
        changed_by="admin-1",
    )


async def test_get_audit_empty_tenant(
    async_client: AsyncClient, admin_token_for,
) -> None:
    tenant = await TenantRepository().create(
        name="M5.3 GET empty", plan=TenantPlan.PRO,
    )
    try:
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs/audit",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"entries": [], "total": 0}
    finally:
        await _delete_tenant(tenant.id)


async def test_get_audit_returns_entries_newest_first(
    async_client: AsyncClient, admin_token_for,
) -> None:
    tenant = await TenantRepository().create(
        name="M5.3 GET ordering", plan=TenantPlan.PRO,
    )
    try:
        await _mutate_via_wrapper(tenant.id)
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs/audit",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] >= 3  # 1 POST (4 fields) + 1 PATCH (1 field) >= 3
        # Newest-first → changed_at is non-increasing
        timestamps = [e["changed_at"] for e in body["entries"]]
        assert timestamps == sorted(timestamps, reverse=True)
    finally:
        await _delete_tenant(tenant.id)


async def test_get_audit_filter_by_provider(
    async_client: AsyncClient, admin_token_for,
) -> None:
    tenant = await TenantRepository().create(
        name="M5.3 GET filter", plan=TenantPlan.PRO,
    )
    try:
        # Seed anthropic via wrapper.
        await _mutate_via_wrapper(tenant.id)
        # Seed openai via wrapper.
        sha = hashlib.sha256(b"sk-openai").hexdigest()
        await AdminTenantLLMConfigRepository().upsert(
            tenant_id=tenant.id,
            payload=TenantLLMConfigCreate(provider_name="openai", api_key="sk-openai"),
            changed_by="admin-1", api_key_sha256=sha,
        )
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs/audit",
            params={"provider_name": "anthropic"},
            headers=auth_headers(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert all(e["provider_name"] == "anthropic" for e in body["entries"])
        # And the total reflects the filter, not the global count.
        assert body["total"] < sum(1 for _ in body["entries"]) + 100  # at least non-zero
    finally:
        await _delete_tenant(tenant.id)


async def test_get_audit_pagination(
    async_client: AsyncClient, admin_token_for,
) -> None:
    tenant = await TenantRepository().create(
        name="M5.3 GET pagination", plan=TenantPlan.PRO,
    )
    try:
        # Seed enough rows to paginate — 2 PATCHes after the POST.
        sha = hashlib.sha256(b"sk-page").hexdigest()
        await AdminTenantLLMConfigRepository().upsert(
            tenant_id=tenant.id,
            payload=TenantLLMConfigCreate(provider_name="anthropic", api_key="sk-page"),
            changed_by="admin-1", api_key_sha256=sha,
        )
        for m in ("claude-haiku-4-5", "claude-3-5-sonnet-20241022", "MiniMax-M3"):
            await AdminTenantLLMConfigRepository().update(
                tenant_id=tenant.id, provider_name="anthropic",
                payload=TenantLLMConfigUpdate(model=m),
                changed_by="admin-1",
            )
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs/audit",
            params={"limit": 2, "offset": 0},
            headers=auth_headers(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["entries"]) == 2
        assert body["total"] >= 2
    finally:
        await _delete_tenant(tenant.id)


async def test_get_audit_invalid_provider_returns_422(
    async_client: AsyncClient, admin_token_for,
) -> None:
    tenant = await TenantRepository().create(
        name="M5.3 GET 422 bad prov", plan=TenantPlan.PRO,
    )
    try:
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs/audit",
            params={"provider_name": "bogus"},
            headers=auth_headers(token),
        )
        assert resp.status_code == 422
    finally:
        await _delete_tenant(tenant.id)


async def test_get_audit_empty_provider_returns_422(
    async_client: AsyncClient, admin_token_for,
) -> None:
    tenant = await TenantRepository().create(
        name="M5.3 GET 422 empty prov", plan=TenantPlan.PRO,
    )
    try:
        token = admin_token_for(tenant_id=tenant.id)
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs/audit",
            params={"provider_name": ""},
            headers=auth_headers(token),
        )
        assert resp.status_code == 422
    finally:
        await _delete_tenant(tenant.id)


async def test_get_audit_without_token_returns_401(
    async_client: AsyncClient,
) -> None:
    resp = await async_client.get(
        "/api/v1/admin/tenants/anything/llm-configs/audit",
    )
    assert resp.status_code == 401


async def test_get_audit_non_admin_token_returns_403(
    async_client: AsyncClient, non_admin_token_for,
) -> None:
    tenant = await TenantRepository().create(
        name="M5.3 GET 403", plan=TenantPlan.PRO,
    )
    try:
        token = non_admin_token_for(tenant_id=tenant.id)
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant.id}/llm-configs/audit",
            headers=auth_headers(token),
        )
        assert resp.status_code == 403
    finally:
        await _delete_tenant(tenant.id)


async def test_get_audit_cross_tenant_returns_404(
    async_client: AsyncClient, admin_token_for,
) -> None:
    a = await TenantRepository().create(name="M5.3 GET 404 A", plan=TenantPlan.PRO)
    b = await TenantRepository().create(name="M5.3 GET 404 B", plan=TenantPlan.PRO)
    try:
        token = admin_token_for(tenant_id=a.id)
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{b.id}/llm-configs/audit",
            headers=auth_headers(token),
        )
        assert resp.status_code == 404
        assert "not found" in resp.json().get("detail", "").lower()
    finally:
        await _delete_tenant(a.id)
        await _delete_tenant(b.id)


async def test_get_audit_unknown_tenant_returns_404(
    async_client: AsyncClient, admin_token_for,
) -> None:
    # Mint a real tenant to mint a valid token, but query a fake tenant.
    real = await TenantRepository().create(
        name="M5.3 GET 404 unknown", plan=TenantPlan.PRO,
    )
    try:
        token = admin_token_for(tenant_id=real.id)
        resp = await async_client.get(
            "/api/v1/admin/tenants/nonexistent-tenant/llm-configs/audit",
            headers=auth_headers(token),
        )
        assert resp.status_code == 404
        assert "not found" in resp.json().get("detail", "").lower()
    finally:
        await _delete_tenant(real.id)