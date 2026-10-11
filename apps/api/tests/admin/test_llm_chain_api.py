"""Integration tests for the admin runtime LLM chain endpoints.

Tier 3 Task 3.4 — Runtime LLM provider chain admin.

Endpoints:
  GET    /api/v1/admin/llm-chain
  PUT    /api/v1/admin/llm-chain

The runtime chain is process-wide; these tests reset the
singleton before/after each test (autouse fixture) to keep state
isolated. We stub ``build_provider_registry`` to return two stub
providers so the tests don't need real API keys.
"""
from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from llm_client.providers.base import BaseProvider
from llm_client.runtime_chain import (
    RuntimeChain,
    reset_runtime_chain_for_tests,
)
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository
from tests.admin.conftest import _delete_tenant, auth_headers


class _StubProvider(BaseProvider):
    """Minimal BaseProvider for tests — no network calls."""

    def __init__(self, name: str, model: str) -> None:
        self.name = name
        self.model = model

    async def chat(self, request: Any) -> Any:  # pragma: no cover - never called
        raise NotImplementedError

    async def stream(self, request: Any):  # pragma: no cover - never called
        if False:
            yield ""


@pytest.fixture(autouse=True)
def _reset_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the runtime chain singleton + stub the provider registry."""
    reset_runtime_chain_for_tests()
    # Replace build_provider_registry with a deterministic stub.
    from admin import api as admin_api

    def _fake_build(settings: Any) -> dict[str, BaseProvider]:
        return {
            "minimax": _StubProvider("minimax", "MiniMax-M3"),
            "anthropic": _StubProvider("anthropic", "claude-3-5-sonnet"),
        }

    monkeypatch.setattr(admin_api, "build_provider_registry", _fake_build)
    yield
    reset_runtime_chain_for_tests()


@pytest.fixture
async def sample_tenant() -> Any:  # AsyncIterator[Tenant]
    tenant = await TenantRepository().create(
        name="LLM Chain Admin Test", plan=TenantPlan.FREE
    )
    try:
        yield tenant
    finally:
        await _delete_tenant(tenant.id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_llm_chain_returns_empty_when_uninitialised(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """No env seed + no admin write → empty chain, providers still listed."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        "/api/v1/admin/llm-chain",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["steps"] == []
    # Providers come from the stubbed registry.
    names = {p["name"] for p in body["providers"]}
    assert names == {"minimax", "anthropic"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_llm_chain_replaces_steps(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    token = admin_token_for(tenant_id=sample_tenant.id)
    payload = {
        "steps": [
            {"provider": "minimax", "model": "MiniMax-M3", "enabled": True},
            {"provider": "anthropic", "model": "claude-3-5-sonnet", "enabled": False},
        ]
    }
    resp = await async_client.put(
        "/api/v1/admin/llm-chain",
        json=payload,
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["steps"]) == 2
    assert body["steps"][0]["provider"] == "minimax"
    assert body["steps"][1]["enabled"] is False

    # Read-back confirms persistence.
    resp2 = await async_client.get(
        "/api/v1/admin/llm-chain",
        headers=auth_headers(token),
    )
    assert resp2.json()["steps"][0]["provider"] == "minimax"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_llm_chain_rejects_unknown_provider(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.put(
        "/api/v1/admin/llm-chain",
        json={"steps": [{"provider": "openai", "model": "x", "enabled": True}]},
        headers=auth_headers(token),
    )
    assert resp.status_code == 422
    assert "openai" in resp.json()["detail"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_llm_chain_rejects_empty_enabled_set(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """All-disabled chain is rejected so the gateway can't silently
    fall back to prefix-only routing."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.put(
        "/api/v1/admin/llm-chain",
        json={
            "steps": [
                {"provider": "minimax", "model": "MiniMax-M3", "enabled": False},
            ]
        },
        headers=auth_headers(token),
    )
    assert resp.status_code == 422
    assert "enabled" in resp.json()["detail"].lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_llm_chain_requires_admin(
    async_client: AsyncClient, sample_tenant: Tenant, non_admin_token_for
) -> None:
    token = non_admin_token_for(tenant_id=sample_tenant.id)
    resp_get = await async_client.get(
        "/api/v1/admin/llm-chain",
        headers=auth_headers(token),
    )
    assert resp_get.status_code == 403
    resp_put = await async_client.put(
        "/api/v1/admin/llm-chain",
        json={"steps": []},
        headers=auth_headers(token),
    )
    assert resp_put.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_runtime_chain_singleton_replaces_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct unit test on the singleton — no HTTP."""
    from llm_client.runtime_chain import ChainStep

    chain = RuntimeChain()
    chain.initial_steps([("minimax", "MiniMax-M3")])
    assert chain.active_chain() == [("minimax", "MiniMax-M3")]

    await chain.replace_steps(
        [
            ChainStep("anthropic", "claude-3-5-sonnet", True),
            ChainStep("minimax", "MiniMax-M3", False),
        ],
        known_providers={"minimax", "anthropic"},
    )
    assert [s.provider for s in chain.snapshot()] == [
        "anthropic",
        "minimax",
    ]
    assert chain.active_chain() == [("anthropic", "claude-3-5-sonnet")]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_runtime_chain_replaces_steps_unknown_provider() -> None:
    from llm_client.runtime_chain import ChainStep

    chain = RuntimeChain()
    chain.initial_steps([("minimax", "MiniMax-M3")])
    with pytest.raises(ValueError, match="unknown provider"):
        await chain.replace_steps(
            [ChainStep("not_registered", "y", True)],
            known_providers={"minimax"},
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_runtime_chain_rejects_empty_enabled() -> None:
    from llm_client.runtime_chain import ChainStep

    chain = RuntimeChain()
    chain.initial_steps([("minimax", "MiniMax-M3")])
    with pytest.raises(ValueError, match="enabled"):
        await chain.replace_steps(
            [ChainStep("minimax", "MiniMax-M3", False)],
            known_providers={"minimax"},
        )