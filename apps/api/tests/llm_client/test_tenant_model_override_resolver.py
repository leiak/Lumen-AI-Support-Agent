"""Unit tests for ``TenantModelOverrideResolver`` (M5.2).

The wrapper is stateless and DB-free; tests use ``create_autospec`` to
build ``BaseProvider`` stubs that carry a ``.name`` attribute and a
``__call__``-compatible inner resolver.

Counter increments are asserted by reading the
``prometheus_client.REGISTRY`` so we don't have to rely on the test
isolation of the underlying Counter object (counters are global to
the process).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, create_autospec

import pytest
from prometheus_client import REGISTRY

from llm_client.providers.base import BaseProvider
from llm_client.tenant_model_override_resolver import (
    TenantModelOverrideResolver,
)
from llm_client.types import ChatMessage, ChatRequest, MessageRole

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_provider(name: str) -> BaseProvider:
    """Return a BaseProvider autospec with the given .name."""
    provider = create_autospec(BaseProvider, instance=True)
    provider.name = name
    return provider


def _request(model: str = "claude-3-5-sonnet-20241022") -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=[ChatMessage(role=MessageRole.USER, content="hello")],
    )


def _counter_value(name: str, labels: dict[str, str]) -> float:
    """Return current value of a labeled Prometheus counter (0.0 if unset)."""
    value = REGISTRY.get_sample_value(name, labels)
    return float(value) if value is not None else 0.0


# ---------------------------------------------------------------------------
# Sync __call__ — happy path / no-rewrite
# ---------------------------------------------------------------------------


def test_call_returns_provider_from_inner_resolver() -> None:
    """Inner resolver returns a provider; wrapper returns the same provider."""
    inner_provider = _stub_provider("anthropic")
    wrapper = TenantModelOverrideResolver(
        inner=lambda req: inner_provider,
        overrides={},
    )

    result = wrapper(_request())

    assert result is inner_provider


def test_call_with_no_override_does_not_rewrite_model() -> None:
    """Empty overrides dict → request.model unchanged, counter outcome=unset."""
    inner_provider = _stub_provider("anthropic")
    wrapper = TenantModelOverrideResolver(
        inner=lambda req: inner_provider,
        overrides={},
    )
    req = _request("claude-3-5-sonnet-20241022")
    before = _counter_value(
        "lumen_llm_tenant_model_override_total",
        {"provider": "anthropic", "outcome": "unset"},
    )

    wrapper(req)

    assert req.model == "claude-3-5-sonnet-20241022"
    after = _counter_value(
        "lumen_llm_tenant_model_override_total",
        {"provider": "anthropic", "outcome": "unset"},
    )
    assert after == before + 1


def test_call_with_override_rewrites_model() -> None:
    """Override for resolved provider → request.model replaced; counter hit fires."""
    inner_provider = _stub_provider("anthropic")
    wrapper = TenantModelOverrideResolver(
        inner=lambda req: inner_provider,
        overrides={"anthropic": "claude-haiku-4-5"},
    )
    req = _request("claude-3-5-sonnet-20241022")
    before = _counter_value(
        "lumen_llm_tenant_model_override_total",
        {"provider": "anthropic", "outcome": "hit"},
    )

    wrapper(req)

    assert req.model == "claude-haiku-4-5"
    after = _counter_value(
        "lumen_llm_tenant_model_override_total",
        {"provider": "anthropic", "outcome": "hit"},
    )
    assert after == before + 1


def test_call_with_unknown_provider_does_not_crash() -> None:
    """Override configured for a different provider → no rewrite, outcome=unset."""
    inner_provider = _stub_provider("anthropic")
    wrapper = TenantModelOverrideResolver(
        inner=lambda req: inner_provider,
        overrides={"openai": "gpt-4o"},
    )
    req = _request("claude-3-5-sonnet-20241022")
    before = _counter_value(
        "lumen_llm_tenant_model_override_total",
        {"provider": "anthropic", "outcome": "unset"},
    )

    wrapper(req)

    assert req.model == "claude-3-5-sonnet-20241022"
    after = _counter_value(
        "lumen_llm_tenant_model_override_total",
        {"provider": "anthropic", "outcome": "unset"},
    )
    assert after == before + 1


def test_call_overrides_dict_defaults_to_empty() -> None:
    """Empty dict literal → no rewrite (sanity check, no ProviderRegistry needed)."""
    inner_provider = _stub_provider("openai")
    wrapper = TenantModelOverrideResolver(
        inner=lambda req: inner_provider,
        overrides={},
    )
    req = _request("gpt-4o")

    wrapper(req)

    assert req.model == "gpt-4o"


def test_call_with_provider_missing_name_is_no_op() -> None:
    """Provider with no ``.name`` attribute → request untouched, no counter fire."""
    inner_provider = create_autospec(BaseProvider, instance=True)
    # Deliberately do NOT assign ``inner_provider.name`` — exercises the
    # defensive ``getattr(provider, "name", None)`` branch.
    wrapper = TenantModelOverrideResolver(
        inner=lambda req: inner_provider,
        overrides={"anthropic": "claude-haiku-4-5"},
    )
    req = _request("claude-3-5-sonnet-20241022")

    wrapper(req)

    assert req.model == "claude-3-5-sonnet-20241022"


# ---------------------------------------------------------------------------
# Async ainvoke — happy path / delegation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ainvoke_delegates_to_inner_async() -> None:
    """Inner resolver with ``ainvoke`` → wrapper returns its result unchanged."""

    sentinel = object()

    class _Inner:
        def __call__(self, request: ChatRequest) -> BaseProvider:
            return _stub_provider("anthropic")

        async def ainvoke(self, request: ChatRequest):
            return sentinel

    wrapper = TenantModelOverrideResolver(
        inner=_Inner(),  # type: ignore[arg-type]
        overrides={"anthropic": "claude-haiku-4-5"},
    )

    result = await wrapper.ainvoke(_request())

    assert result is sentinel


@pytest.mark.asyncio
async def test_ainvoke_rewrites_model_before_delegation() -> None:
    """Wrapper rewrites ``request.model`` BEFORE calling ``inner.ainvoke``."""

    class _Inner:
        def __call__(self, request: ChatRequest) -> BaseProvider:
            return _stub_provider("anthropic")

        async def ainvoke(self, request: ChatRequest) -> str:
            # Capture the request the wrapper actually delegated.
            return request.model

    wrapper = TenantModelOverrideResolver(
        inner=_Inner(),  # type: ignore[arg-type]
        overrides={"anthropic": "claude-haiku-4-5"},
    )

    model_seen_by_inner = await wrapper.ainvoke(_request("claude-3-5-sonnet-20241022"))

    assert model_seen_by_inner == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_ainvoke_falls_back_to_performing_chat_when_inner_has_no_ainvoke() -> None:
    """Inner resolver is plain lambda (no ``ainvoke``) → ``ainvoke`` performs the chat itself.

    When the inner resolver has no async path (e.g. plain ``_PrefixResolver``),
    the wrapper drives ``provider.chat(request)`` itself so the caller
    (``LLMClient``) always receives a ``ChatResponse``, never a bare
    ``BaseProvider``. Surfaced as a real bug on 2026-10-13 when the
    demo tenant pointed at the Doubao Ark endpoint — the previous
    "return the provider" fallback caused ``LLMClient`` to AttributeError
    on ``resp.prompt_tokens``. This test pins the new contract.
    """
    inner_provider = _stub_provider("anthropic")
    expected_response = object()  # sentinel returned from provider.chat
    inner_provider.chat = AsyncMock(return_value=expected_response)  # type: ignore[method-assign]

    wrapper = TenantModelOverrideResolver(
        inner=lambda req: inner_provider,
        overrides={},
    )

    result = await wrapper.ainvoke(_request())

    assert result is expected_response
    inner_provider.chat.assert_awaited_once()