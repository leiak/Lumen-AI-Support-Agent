"""Unit tests for llm_client.resolvers."""
from unittest.mock import create_autospec

import pytest

from llm_client.providers.base import BaseProvider
from llm_client.resolvers import PinnedResolver, UnknownModelError, _PrefixResolver
from llm_client.types import ChatMessage, ChatRequest, MessageRole


def _stub_provider(name: str) -> BaseProvider:
    """Build an isinstance-compatible BaseProvider stub for tests."""
    p = create_autospec(BaseProvider, instance=True)
    p.name = name
    return p


def test_pinned_resolver_returns_pinned_provider_for_any_request() -> None:
    pinned = _stub_provider("minimax")
    r = PinnedResolver(provider=pinned, model="MiniMax-M3")
    req = ChatRequest(
        model="ignored",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    assert r(req) is pinned


def test_pinned_resolver_exposes_model_attribute() -> None:
    pinned = _stub_provider("minimax")
    r = PinnedResolver(provider=pinned, model="MiniMax-M3")
    assert r.model == "MiniMax-M3"
    assert r.provider is pinned


def test_prefix_resolver_routes_minimax_to_minimax_provider() -> None:
    minimax = _stub_provider("minimax")
    anthropic = _stub_provider("anthropic")
    r = _PrefixResolver(
        providers={"minimax": minimax, "anthropic": anthropic},
        default_provider_name="anthropic",
    )
    req = ChatRequest(
        model="MiniMax-M3",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    assert r(req) is minimax


def test_prefix_resolver_routes_claude_to_anthropic() -> None:
    anthropic = _stub_provider("anthropic")
    openai = _stub_provider("openai")
    r = _PrefixResolver(
        providers={"anthropic": anthropic, "openai": openai},
        default_provider_name="anthropic",
    )
    req = ChatRequest(
        model="claude-sonnet-4-5",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    assert r(req) is anthropic


def test_prefix_resolver_routes_openai_prefix_to_openai() -> None:
    openai = _stub_provider("openai")
    anthropic = _stub_provider("anthropic")
    r = _PrefixResolver(
        providers={"openai": openai, "anthropic": anthropic},
        default_provider_name="anthropic",
    )
    for model in ("gpt-4o-mini", "o1-preview", "o3-mini"):
        req = ChatRequest(
            model=model,
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
        )
        assert r(req) is openai, f"prefix {model!r} should route to openai"


def test_prefix_resolver_falls_back_to_default_for_unknown_prefix() -> None:
    anthropic = _stub_provider("anthropic")
    r = _PrefixResolver(
        providers={"anthropic": anthropic},
        default_provider_name="anthropic",
    )
    req = ChatRequest(
        model="some-custom-model",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    assert r(req) is anthropic


def test_prefix_resolver_raises_UnknownModelError_when_no_default() -> None:
    minimax = _stub_provider("minimax")
    r = _PrefixResolver(
        providers={"minimax": minimax},
        default_provider_name="openai",
    )
    req = ChatRequest(
        model="mystery-model",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    with pytest.raises(UnknownModelError):
        r(req)


def test_prefix_resolver_uses_default_when_prefix_family_unregistered() -> None:
    """A model like 'gpt-4o' with no OpenAI registered falls back to default.

    The spec says unknown prefix → default. If the prefix is recognized
    but that family isn't registered, we treat it like unknown — same
    fallback. (We don't 4xx just because OpenAI is unset.)
    """
    anthropic = _stub_provider("anthropic")
    r = _PrefixResolver(
        providers={"anthropic": anthropic},
        default_provider_name="anthropic",
    )
    req = ChatRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    assert r(req) is anthropic