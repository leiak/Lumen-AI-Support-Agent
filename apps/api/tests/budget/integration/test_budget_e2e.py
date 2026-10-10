"""End-to-end tests for the M4.D Budget Layer via pytest-httpx.

M4.D Task 5 — exercises the full resolver chain:

    LLMClient -> BudgetResolver -> TenantResolver -> LLMGateway -> BaseProvider -> httpx

All tests use ``_default_llm_client_factory`` so the production wiring
(including the factory's opt-in branch) is exercised end-to-end. HTTP
traffic is mocked via ``pytest-httpx`` — a request that fails gets caught
by ``len(httpx_mock.get_requests()) == 0`` in the cap-block test.

Marked ``@pytest.mark.integration`` so the default selector
(``pytest -m "not integration"``) skips it, matching the existing
``tests/llm_client/integration/test_*.py`` convention.

PII discipline: plaintext keys in the test bodies are synthetic
placeholder strings (``"sk-tenant-*"``) — never real customer
credentials.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pytest_httpx import HTTPXMock

from agent import llm_factory as llm_factory_module
from agent.llm_factory import _default_llm_client_factory
from budget.repository import (
    TenantBudgetRepository,
    TenantBudgetSnapshotRepository,
)
from budget.resolver import BudgetResolver, _current_period
from llm_client.exceptions import TenantBudgetExceeded
from llm_client.tenant_config_crypto import TenantLLMConfigCipher
from llm_client.tenant_config_models import TenantLLMConfigRepository
from llm_client.types import ChatMessage, ChatRequest, MessageRole
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_db_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset DB engine / sessionmaker / settings / factory caches per test.

    Each ``pytest-asyncio`` test gets its own event loop. Without
    resetting the SQLAlchemy engine, the previous test's closed loop
    would be reused and raise "Event loop is closed". Without resetting
    settings, a fresh ``TENANT_LLM_FERNET_KEY`` via ``monkeypatch.setenv``
    wouldn't reach ``get_settings()`` (which caches the result on first
    call). Without resetting the factory caches, the cipher / snapshot
    cache constructed from an older Fernet key would be reused and
    decryption / tenant_budget lookup would leak across cases.

    Also disables global provider keys so the factory genuinely only
    uses the tenant's BYOK config.
    """
    fernet_key = Fernet.generate_key().decode()
    monkeypatch.setenv("TENANT_LLM_FERNET_KEY", fernet_key)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("MINIMAX_API_KEY", "")
    # Force a single-step fallback chain so the factory's inner resolver
    # is a FallbackResolver (which exposes ``ainvoke``). Without this,
    # the inner resolver is a _PrefixResolver — BudgetResolver.ainvoke
    # would then fail with AttributeError because _PrefixResolver has no
    # ainvoke. See known-tech-debt follow-up for the non-Fallback inner.
    monkeypatch.setenv(
        "LLM_FALLBACK_CHAIN", "anthropic:claude-3-5-sonnet-20241022",
    )

    from core.config import reset_settings
    from core.database import reset_engine, reset_sessionmaker

    reset_settings()
    reset_engine()
    reset_sessionmaker()
    llm_factory_module._tenant_cache = None
    llm_factory_module._budget_snapshot_cache = None
    yield
    reset_settings()
    reset_engine()
    reset_sessionmaker()
    llm_factory_module._tenant_cache = None
    llm_factory_module._budget_snapshot_cache = None


async def _delete_tenant(tenant_id: str) -> None:
    """Cascade-delete a tenant row."""
    from core.database import get_session

    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t is not None:
            await session.delete(t)
            await session.commit()


def _request() -> ChatRequest:
    """Standard test request — matches the fallback e2e convention."""
    return ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )


def _anthropic_ok_response(
    prompt_tokens: int = 10, completion_tokens: int = 5
) -> dict:
    """Anthropic /v1/messages response body in the shape the adapter parses."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-3-5-sonnet-20241022",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": prompt_tokens, "output_tokens": completion_tokens},
    }


async def _seed_anthropic_byok_async(tenant_id: str, *, plaintext_key: str) -> None:
    """Upsert a tenant_llm_configs row with an Anthropic BYOK key.

    Reads ``TENANT_LLM_FERNET_KEY`` from env (set by the autouse fixture
    above) so the cipher matches what the factory's
    ``_build_tenant_cache`` will construct.
    """
    from core.config import get_settings

    cipher = TenantLLMConfigCipher(get_settings().tenant_llm_fernet_key)
    await TenantLLMConfigRepository().upsert(
        tenant_id=tenant_id,
        provider_name="anthropic",
        encrypted_api_key=cipher.encrypt(plaintext_key),
        base_url=None,
        enabled=True,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_e2e_cap_blocks_provider_http(
    httpx_mock: HTTPXMock,
) -> None:
    """Hard cap exhausted → ``TenantBudgetExceeded`` BEFORE any HTTP call.

    Pre-seed a ``tenant_budgets`` row (hard_cap=10) and a current-period
    snapshot with ``tokens_used=10`` (== cap). The factory wraps the
    resolver with ``BudgetResolver``. Calling ``client.chat()`` must raise
    :class:`TenantBudgetExceeded` and make ZERO provider HTTP requests.

    Load-bearing invariant: a rejected request never spends a token. If
    the test ever hits the network, the pre-check is broken (resolver
    is delegating before the cap check) — bug.
    """
    tenant = await TenantRepository().create(
        name="E2E Budget Cap", plan=TenantPlan.PRO,
    )
    try:
        await _seed_anthropic_byok_async(
            tenant.id, plaintext_key="sk-tenant-budget-cap",
        )
        await TenantBudgetRepository().upsert(
            tenant_id=tenant.id,
            soft_warn_tokens=None,
            hard_cap_tokens=10,
        )
        period, _ = _current_period("UTC")
        await TenantBudgetSnapshotRepository().set_tokens_used(
            tenant_id=tenant.id,
            period=period,
            tokens_used=10,  # == hard_cap → cap blocks
        )

        # NOTE: httpx_mock intentionally has NO registered response.
        # If the resolver delegates through to the provider, the mock
        # would raise NoMockAddress. The test would then fail loudly
        # — which is the desired signal that cap-check ordering broke.
        client = await _default_llm_client_factory(tenant_id=tenant.id)
        try:
            with pytest.raises(TenantBudgetExceeded) as exc_info:
                await client.chat(_request())

            assert exc_info.value.tenant_id == tenant.id
            assert exc_info.value.tokens_used == 10
            assert exc_info.value.hard_cap_tokens == 10
            assert exc_info.value.period == period

            # CRITICAL invariant: zero provider HTTP requests.
            assert len(httpx_mock.get_requests()) == 0, (
                "cap check must reject BEFORE provider HTTP — "
                f"observed {len(httpx_mock.get_requests())} request(s)"
            )
        finally:
            await client.aclose()
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
async def test_e2e_tenant_without_budget_passes_through(
    httpx_mock: HTTPXMock,
) -> None:
    """Tenant with NO ``tenant_budgets`` row → factory does NOT wrap with BudgetResolver.

    Opt-in semantics. Without a budget row, ``_default_llm_client_factory``
    skips the ``BudgetResolver`` wrap (preserves M4.C strict-mode behaviour
    for existing tenants). The mocked Anthropic response returns through
    the full chain: ``LLMClient`` → ``TenantResolver`` → ``LLMGateway``
    → ``AnthropicProvider`` → ``httpx``.
    """
    tenant = await TenantRepository().create(
        name="E2E No Budget", plan=TenantPlan.PRO,
    )
    try:
        await _seed_anthropic_byok_async(
            tenant.id, plaintext_key="sk-tenant-no-budget",
        )
        # Deliberately NO TenantBudgetRepository().upsert(...) call here.
        # The factory's ``get_by_tenant`` will return ``None`` → no BudgetResolver.

        httpx_mock.add_response(
            url="https://api.anthropic.com/v1/messages",
            method="POST",
            json=_anthropic_ok_response(prompt_tokens=5, completion_tokens=3),
            status_code=200,
        )

        client = await _default_llm_client_factory(tenant_id=tenant.id)
        try:
            # Verify the factory's opt-in: provider_resolver is NOT a BudgetResolver.
            assert not isinstance(client.provider_resolver, BudgetResolver), (
                "factory must skip BudgetResolver wrap when no "
                "tenant_budgets row exists (opt-in per tenant)"
            )

            resp = await client.chat(_request())
            assert resp.content == "ok"
            assert resp.prompt_tokens == 5
            assert resp.completion_tokens == 3

            # HTTP call WAS made (call went through to the provider).
            requests = httpx_mock.get_requests()
            assert len(requests) == 1, (
                f"expected 1 HTTP call, got {len(requests)}"
            )
            # Sanity: the tenant's BYOK plaintext key reaches the
            # Authorization header — confirms the chain did NOT bypass
            # the TenantResolver (we want full coverage, not a stub).
            assert (
                requests[0].headers.get("x-api-key") == "sk-tenant-no-budget"
            )
        finally:
            await client.aclose()
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
async def test_e2e_month_rollover_resets_budget(
    httpx_mock: HTTPXMock,
) -> None:
    """Old-period snapshot exhausted → current-period snapshot fresh → call proceeds.

    The cache is keyed by ``(tenant_id, period)`` so each period has its
    own row. An exhausted previous-period snapshot does NOT block the
    current period; the resolver only checks the current period's row.

    Pre-seed:
    - ``2020-01`` snapshot at hard_cap (simulating last month's exhaustion)
    - current-period snapshot at 0 (fresh)

    Expected:
    - ``chat()`` succeeds (no cap block in current period)
    - 1 provider HTTP call
    - current-period snapshot is incremented to ``prompt+completion``
    - old-period snapshot is unchanged
    """
    tenant = await TenantRepository().create(
        name="E2E Rollover", plan=TenantPlan.PRO,
    )
    try:
        await _seed_anthropic_byok_async(
            tenant.id, plaintext_key="sk-tenant-rollover",
        )
        await TenantBudgetRepository().upsert(
            tenant_id=tenant.id,
            soft_warn_tokens=None,
            hard_cap_tokens=10,
        )

        period, _ = _current_period("UTC")
        snapshot_repo = TenantBudgetSnapshotRepository()
        # Old period at cap (simulating exhausted previous month)
        await snapshot_repo.set_tokens_used(
            tenant_id=tenant.id,
            period="2020-01",
            tokens_used=10,
        )
        # Current period fresh at 0
        await snapshot_repo.set_tokens_used(
            tenant_id=tenant.id,
            period=period,
            tokens_used=0,
        )

        httpx_mock.add_response(
            url="https://api.anthropic.com/v1/messages",
            method="POST",
            json=_anthropic_ok_response(prompt_tokens=10, completion_tokens=5),
            status_code=200,
        )

        client = await _default_llm_client_factory(tenant_id=tenant.id)
        try:
            resp = await client.chat(_request())
            assert resp.content == "ok"
            assert resp.prompt_tokens == 10
            assert resp.completion_tokens == 5

            # HTTP call was made — current period is fresh.
            assert len(httpx_mock.get_requests()) == 1

            # Current-period snapshot was incremented (0 + 10 + 5 = 15).
            current_snap = await snapshot_repo.get_for_tenant_period(
                tenant_id=tenant.id, period=period,
            )
            assert current_snap is not None
            assert current_snap.tokens_used == 15, (
                f"expected current-period snapshot to be incremented to 15, "
                f"got {current_snap.tokens_used}"
            )

            # Old-period snapshot unchanged — proves periods are independent.
            old_snap = await snapshot_repo.get_for_tenant_period(
                tenant_id=tenant.id, period="2020-01",
            )
            assert old_snap is not None
            assert old_snap.tokens_used == 10, (
                "old-period snapshot must not be touched by a current-period call"
            )
        finally:
            await client.aclose()
    finally:
        await _delete_tenant(tenant.id)


__all__ = [
    "test_e2e_cap_blocks_provider_http",
    "test_e2e_month_rollover_resets_budget",
    "test_e2e_tenant_without_budget_passes_through",
]