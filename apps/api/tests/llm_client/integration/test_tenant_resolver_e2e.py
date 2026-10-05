"""End-to-end tests for TenantResolver with real HTTP traffic via pytest-httpx.

M4.C Task 5 — exercises the full per-tenant resolver path:

    _default_llm_client_factory(tenant_id)
      -> build_tenant_resolver
      -> TenantLLMConfigCache.get_or_load
      -> TenantLLMConfigRepository.list_by_tenant
      -> TenantLLMConfigCipher.decrypt
      -> LLMGateway(default_resolver=...)
      -> LLMClient.chat(ChatRequest)
      -> AnthropicProvider.chat (httpx)
      -> x-api-key header MUST equal the tenant's decrypted key

Goal: verify the tenant's decrypted API key is what reaches the upstream
HTTP client (no global project keys leaking in), and that strict-mode
errors surface from the factory without any HTTP traffic.

PII discipline: plaintext keys in the test bodies are synthetic
placeholder strings (``"sk-tenant-anthropic-secret"`` etc.) — never
real customer credentials.

Marked ``@pytest.mark.integration`` so the default selector
(``pytest -m "not integration"``) skips it, matching the existing
``test_gateway_e2e.py`` convention.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from cryptography.fernet import Fernet
from pytest_httpx import HTTPXMock

from agent import llm_factory as llm_factory_module
from agent.llm_factory import _default_llm_client_factory
from llm_client.exceptions import TenantLlmNotConfigured
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
def _reset_db_singletons() -> None:
    """Reset DB engine/sessionmaker + settings + tenant cache for isolation.

    Each ``pytest-asyncio`` test gets its own event loop. Without
    resetting the SQLAlchemy engine, the previous test's closed loop
    would be reused and raise "Event loop is closed". Without
    resetting settings, a fresh ``TENANT_LLM_FERNET_KEY`` via
    ``monkeypatch.setenv`` wouldn't reach ``get_settings()`` (which
    caches the result on first call). Without resetting the tenant
    cache singleton, the cipher constructed from an older fernet key
    would be reused and decryption would fail.
    """
    from core.config import reset_settings
    from core.database import reset_engine, reset_sessionmaker

    reset_settings()
    reset_engine()
    reset_sessionmaker()
    llm_factory_module._tenant_cache = None
    yield
    reset_settings()
    reset_engine()
    reset_sessionmaker()
    llm_factory_module._tenant_cache = None


async def _delete_tenant(tenant_id: str) -> None:
    """Cascade-delete a tenant row."""
    from core.database import get_session

    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t is not None:
            await session.delete(t)
            await session.commit()


@pytest.fixture
async def tenant_with_anthropic_key(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[Tenant, str], None]:
    """Create a tenant + upsert a single Anthropic BYOK config.

    Yields ``(tenant, plaintext_api_key)`` so the test can assert on
    the plaintext value reaching the mock HTTP layer.
    """
    # Force a fresh Fernet key for this test so cipher / cache don't
    # leak from a prior test (which used a different fernet key).
    fernet_key = Fernet.generate_key().decode()
    monkeypatch.setenv("TENANT_LLM_FERNET_KEY", fernet_key)
    # Disable any global provider keys so the AnthropicProvider inside
    # the resolver genuinely only has the tenant's decrypted key as a
    # candidate. The provider only stores it as ``x-api-key``; the
    # global keys are not used by ``AnthropicProvider.__init__`` but
    # we clear them so other providers aren't accidentally constructed
    # via the fallback chain.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("MINIMAX_API_KEY", "")

    plaintext = "sk-tenant-anthropic-secret"
    cipher = TenantLLMConfigCipher(fernet_key)
    tenant = await TenantRepository().create(
        name="E2E Antr Key", plan=TenantPlan.PRO
    )
    await TenantLLMConfigRepository().upsert(
        tenant_id=tenant.id,
        provider_name="anthropic",
        encrypted_api_key=cipher.encrypt(plaintext),
        base_url=None,
        enabled=True,
    )
    try:
        yield tenant, plaintext
    finally:
        await _delete_tenant(tenant.id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_e2e_tenant_key_used_in_anthropic_request(
    httpx_mock: HTTPXMock,
    tenant_with_anthropic_key: tuple[Tenant, str],
) -> None:
    """The tenant's decrypted key is what reaches Anthropic's HTTP client.

    Critical invariant: the resolver must decrypt the BYOK row and
    pass that plaintext (NOT a project-wide env key) into the
    AnthropicProvider's ``x-api-key`` header. If the header carries
    anything else (empty, a different key, a global env value), the
    tenant's traffic would be charged to someone else's account.
    """
    tenant, plaintext_key = tenant_with_anthropic_key

    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-5-sonnet-20241022",
            "content": [{"type": "text", "text": "hello from anthropic"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
        status_code=200,
    )

    client = await _default_llm_client_factory(tenant_id=tenant.id)
    try:
        resp = await client.chat(
            ChatRequest(
                model="claude-3-5-sonnet-20241022",
                messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            )
        )
    finally:
        await client.aclose()

    assert resp.content == "hello from anthropic"

    # Verify the request used the tenant's key (not a global key).
    requests = httpx_mock.get_requests()
    assert len(requests) == 1, f"expected 1 HTTP call, got {len(requests)}"
    auth_header = requests[0].headers.get("x-api-key", "")
    assert auth_header == plaintext_key, (
        f"x-api-key must equal the tenant's decrypted BYOK key; got {auth_header!r}"
    )


@pytest.mark.integration
async def test_e2e_tenant_not_configured_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strict mode: tenant with zero enabled configs → TenantLlmNotConfigured.

    Verifies that the factory raises the typed exception at factory
    time, BEFORE any provider client is constructed or HTTP call is
    attempted. The exception must carry the ``tenant_id`` so the API
    layer can map it to a 503-style response.
    """
    fernet_key = Fernet.generate_key().decode()
    monkeypatch.setenv("TENANT_LLM_FERNET_KEY", fernet_key)

    tenant = await TenantRepository().create(name="E2E No Key", plan=TenantPlan.PRO)
    try:
        with pytest.raises(TenantLlmNotConfigured) as exc_info:
            await _default_llm_client_factory(tenant_id=tenant.id)
        assert exc_info.value.tenant_id == tenant.id
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
async def test_e2e_tenant_key_isolation(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two tenants each with their own key — verify no cross-leak.

    Resolver caching could (theoretically) leak keys across tenants
    if the inner ``AnthropicProvider`` instances were shared. This
    test sets up two tenants with distinct keys, makes one call each,
    and asserts that the recorded ``x-api-key`` headers match the
    owning tenant.
    """
    fernet_key = Fernet.generate_key().decode()
    monkeypatch.setenv("TENANT_LLM_FERNET_KEY", fernet_key)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("MINIMAX_API_KEY", "")

    cipher = TenantLLMConfigCipher(fernet_key)
    t1 = await TenantRepository().create(name="E2E Iso 1", plan=TenantPlan.PRO)
    t2 = await TenantRepository().create(name="E2E Iso 2", plan=TenantPlan.PRO)
    repo = TenantLLMConfigRepository()
    await repo.upsert(
        tenant_id=t1.id,
        provider_name="anthropic",
        encrypted_api_key=cipher.encrypt("sk-tenant-1-key"),
        base_url=None,
        enabled=True,
    )
    await repo.upsert(
        tenant_id=t2.id,
        provider_name="anthropic",
        encrypted_api_key=cipher.encrypt("sk-tenant-2-key"),
        base_url=None,
        enabled=True,
    )

    # Pre-register two responses; we'll inspect each by index.
    for _ in range(2):
        httpx_mock.add_response(
            url="https://api.anthropic.com/v1/messages",
            method="POST",
            json={
                "id": "msg",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-5-sonnet-20241022",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            status_code=200,
        )

    try:
        # First call — tenant 1. The resolver cache is shared per
        # process; this exercises the cache-hit + cache-miss paths in
        # the same test.
        client_t1 = await _default_llm_client_factory(tenant_id=t1.id)
        await client_t1.chat(
            ChatRequest(
                model="claude-3-5-sonnet-20241022",
                messages=[ChatMessage(role=MessageRole.USER, content="hi t1")],
            )
        )
        await client_t1.aclose()

        client_t2 = await _default_llm_client_factory(tenant_id=t2.id)
        await client_t2.chat(
            ChatRequest(
                model="claude-3-5-sonnet-20241022",
                messages=[ChatMessage(role=MessageRole.USER, content="hi t2")],
            )
        )
        await client_t2.aclose()
    finally:
        await _delete_tenant(t1.id)
        await _delete_tenant(t2.id)

    requests = httpx_mock.get_requests()
    assert len(requests) == 2, f"expected 2 HTTP calls, got {len(requests)}"
    # First call should carry tenant 1's key.
    assert requests[0].headers.get("x-api-key") == "sk-tenant-1-key"
    # Second call should carry tenant 2's key (no cross-leak).
    assert requests[1].headers.get("x-api-key") == "sk-tenant-2-key"


__all__ = [
    "test_e2e_tenant_key_used_in_anthropic_request",
    "test_e2e_tenant_not_configured_returns_error",
    "test_e2e_tenant_key_isolation",
]
