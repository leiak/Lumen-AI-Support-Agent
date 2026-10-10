"""Tests for LLMGateway default_fallback_chain + parse_fallback_chain_env."""
from __future__ import annotations

from unittest.mock import create_autospec

import pytest

from llm_client.gateway import LLMGateway
from llm_client.provider_registry import parse_fallback_chain_env
from llm_client.providers.base import BaseProvider
from llm_client.resolvers import FallbackResolver, _PrefixResolver
from llm_client.types import ChatMessage, ChatRequest, MessageRole


def _stub(name: str) -> BaseProvider:
    p = create_autospec(BaseProvider, instance=True)
    p.name = name
    return p


def _request() -> ChatRequest:
    return ChatRequest(
        model="MiniMax-M3",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )


# ---- parser ----


def test_parse_none_returns_empty_list() -> None:
    assert parse_fallback_chain_env(None) == []


def test_parse_empty_string_returns_empty_list() -> None:
    assert parse_fallback_chain_env("") == []


def test_parse_single_step() -> None:
    assert parse_fallback_chain_env("minimax:MiniMax-M3") == [
        ("minimax", "MiniMax-M3")
    ]


def test_parse_two_steps() -> None:
    assert parse_fallback_chain_env(
        "minimax:MiniMax-M3,anthropic:claude-haiku-4-5"
    ) == [
        ("minimax", "MiniMax-M3"),
        ("anthropic", "claude-haiku-4-5"),
    ]


def test_parse_strips_whitespace() -> None:
    assert parse_fallback_chain_env(
        " minimax:MiniMax-M3 , anthropic:claude-haiku-4-5 "
    ) == [
        ("minimax", "MiniMax-M3"),
        ("anthropic", "claude-haiku-4-5"),
    ]


def test_parse_rejects_entry_without_colon() -> None:
    with pytest.raises(ValueError, match="expected 'provider:model'"):
        parse_fallback_chain_env("minimax-no-colon")


def test_parse_rejects_empty_provider() -> None:
    with pytest.raises(ValueError, match="empty provider"):
        parse_fallback_chain_env(":MiniMax-M3")


def test_parse_rejects_empty_model() -> None:
    with pytest.raises(ValueError, match="empty model"):
        parse_fallback_chain_env("minimax:")


# ---- LLMGateway fallback chain ----


def test_gateway_no_chain_uses_prefix_resolver() -> None:
    p = _stub("minimax")
    g = LLMGateway(providers={"minimax": p})
    # Default resolver still routes by prefix; not a FallbackResolver.
    assert isinstance(g.default_resolver, _PrefixResolver)
    assert not isinstance(g.default_resolver, FallbackResolver)


def test_gateway_with_chain_builds_fallback_resolver() -> None:
    minimax = _stub("minimax")
    anthropic = _stub("anthropic")
    g = LLMGateway(
        providers={"minimax": minimax, "anthropic": anthropic},
        default_fallback_chain=[
            ("minimax", "MiniMax-M3"),
            ("anthropic", "claude-haiku-4-5"),
        ],
    )
    assert isinstance(g.default_resolver, FallbackResolver)
    # First call returns primary step's provider (placeholder for stream_chat).
    assert g.default_resolver(_request()) is minimax


def test_gateway_skips_steps_with_unregistered_provider() -> None:
    """Steps referencing providers not in the registry are dropped, not fatal.

    Rationale: an operator might enable fallback env in a deployment
    that only has MiniMax configured — we don't want the API to refuse
    to boot. The chain runs with whatever subset is available.
    """
    minimax = _stub("minimax")
    g = LLMGateway(
        providers={"minimax": minimax},
        default_fallback_chain=[
            ("minimax", "MiniMax-M3"),
            ("anthropic", "claude-haiku-4-5"),  # not registered
        ],
    )
    assert isinstance(g.default_resolver, FallbackResolver)
    assert len(g.default_resolver.steps) == 1
    assert g.default_resolver.steps[0].provider is minimax


def test_gateway_raises_when_no_steps_resolve() -> None:
    """All chain entries reference unregistered providers → ValueError at boot."""
    minimax = _stub("minimax")
    with pytest.raises(ValueError, match="at least one step"):
        LLMGateway(
            providers={"minimax": minimax},
            default_fallback_chain=[("anthropic", "claude-haiku-4-5")],
        )