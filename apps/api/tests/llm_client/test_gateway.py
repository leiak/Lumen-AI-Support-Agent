"""Unit tests for llm_client.gateway.LLMGateway."""
from unittest.mock import AsyncMock, create_autospec

import pytest

from llm_client.gateway import LLMGateway
from llm_client.providers.base import BaseProvider
from llm_client.resolvers import PinnedResolver, UnknownModelError
from llm_client.types import ChatMessage, ChatRequest, MessageRole


def _stub(name: str) -> BaseProvider:
    p = create_autospec(BaseProvider, instance=True)
    p.name = name
    # ``BaseProvider`` does not declare ``aclose`` (only the concrete
    # Anthropic/OpenAI adapters do), so ``create_autospec`` doesn't
    # auto-mock it. Attach one explicitly for tests that exercise the
    # gateway's ``aclose_all`` path.
    p.aclose = AsyncMock()  # type: ignore[attr-defined]
    return p


def _stub_request(model: str) -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )


def test_gateway_stores_providers_and_default() -> None:
    a = _stub("anthropic")
    m = _stub("minimax")
    g = LLMGateway(providers={"anthropic": a, "minimax": m})
    assert g.providers == {"anthropic": a, "minimax": m}
    assert g.default_provider_name == "anthropic"  # first insertion order


def test_gateway_explicit_default_provider_name() -> None:
    a = _stub("anthropic")
    m = _stub("minimax")
    g = LLMGateway(
        providers={"anthropic": a, "minimax": m},
        default_provider_name="minimax",
    )
    assert g.default_provider_name == "minimax"


def test_gateway_raises_when_providers_empty() -> None:
    with pytest.raises(RuntimeError, match="at least one provider"):
        LLMGateway(providers={})


def test_gateway_resolve_routes_by_model_prefix() -> None:
    a = _stub("anthropic")
    m = _stub("minimax")
    g = LLMGateway(providers={"anthropic": a, "minimax": m})
    assert g.resolve(_stub_request("claude-sonnet-4-5")) is a
    assert g.resolve(_stub_request("MiniMax-M3")) is m


def test_gateway_default_resolver_returns_callable() -> None:
    a = _stub("anthropic")
    g = LLMGateway(providers={"anthropic": a})
    resolver = g.default_resolver
    assert resolver(_stub_request("anything")) is a


def test_gateway_with_config_returns_pinned_resolver() -> None:
    a = _stub("anthropic")
    m = _stub("minimax")
    g = LLMGateway(providers={"anthropic": a, "minimax": m})
    pinned = g.with_config(provider="minimax", model="MiniMax-M3")
    assert isinstance(pinned, PinnedResolver)
    # And it ignores the request's model field:
    assert pinned(_stub_request("claude-sonnet-4-5")) is m
    assert pinned.model == "MiniMax-M3"


def test_gateway_with_config_raises_for_unknown_provider() -> None:
    a = _stub("anthropic")
    g = LLMGateway(providers={"anthropic": a})
    with pytest.raises(ValueError, match="not registered"):
        g.with_config(provider="openai", model="gpt-4o")


def test_gateway_resolve_propagates_UnknownModelError() -> None:
    a = _stub("anthropic")
    g = LLMGateway(providers={"anthropic": a}, default_provider_name="openai")
    with pytest.raises(UnknownModelError):
        g.resolve(_stub_request("mystery-model"))


@pytest.mark.asyncio
async def test_gateway_aclose_all_closes_each_provider() -> None:
    a = _stub("anthropic")
    m = _stub("minimax")
    g = LLMGateway(providers={"anthropic": a, "minimax": m})
    await g.aclose_all()
    a.aclose.assert_awaited_once()  # type: ignore[attr-defined]
    m.aclose.assert_awaited_once()  # type: ignore[attr-defined]


def test_gateway_providers_property_is_read_only_view() -> None:
    a = _stub("anthropic")
    g = LLMGateway(providers={"anthropic": a})
    snapshot = g.providers
    # Mutating the returned mapping does not affect the gateway:
    # (raises TypeError on MappingProxyType, but we catch + assert no leak)
    try:
        snapshot["openai"] = _stub("openai")  # type: ignore[index]
    except TypeError:
        pass  # expected — MappingProxyType is immutable
    assert "openai" not in g.providers


@pytest.mark.asyncio
async def test_gateway_aclose_all_skips_provider_without_aclose() -> None:
    """A provider stub without an ``aclose`` method must not raise."""
    from unittest.mock import create_autospec
    a = _stub("anthropic")
    # Strip aclose from one stub to exercise the getattr(..., None) branch
    if hasattr(a, "aclose"):
        del a.aclose  # type: ignore[attr-defined]
    g = LLMGateway(providers={"anthropic": a})
    # Must not raise even though one provider has no aclose
    await g.aclose_all()


@pytest.mark.asyncio
async def test_gateway_aclose_all_isolates_one_failing_provider() -> None:
    """One provider raising in aclose() must not block the others."""
    from unittest.mock import AsyncMock

    a = _stub("anthropic")
    m = _stub("minimax")
    a.aclose = AsyncMock(side_effect=RuntimeError("anthropic boom"))
    m.aclose = AsyncMock()  # succeeds
    g = LLMGateway(providers={"anthropic": a, "minimax": m})
    # Must not raise; both providers should have been attempted.
    await g.aclose_all()
    a.aclose.assert_awaited_once()  # type: ignore[attr-defined]
    m.aclose.assert_awaited_once()  # type: ignore[attr-defined]
