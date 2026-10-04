"""Unit tests for llm_client.resolvers.FallbackResolver."""
from __future__ import annotations

import asyncio
from unittest.mock import create_autospec

import pytest

from llm_client.exceptions import (
    FallbackChainExhausted,
    InvalidRequest,
    OutputInvalid,
    ProviderUnavailable,
    RateLimited,
)
from llm_client.providers.base import BaseProvider
from llm_client.resolvers import FallbackResolver, PinnedResolver
from llm_client.types import ChatMessage, ChatRequest, ChatResponse, MessageRole


def _stub_provider(name: str) -> BaseProvider:
    """isinstance-compatible BaseProvider stub with a settable chat side_effect."""
    p = create_autospec(BaseProvider, instance=True)
    p.name = name
    return p


def _request(model: str = "anything") -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )


def _response(provider_name: str, model: str) -> ChatResponse:
    return ChatResponse(
        content="ok",
        model=model,
        prompt_tokens=5,
        completion_tokens=3,
        finish_reason="stop",
        provider_name=provider_name,
    )


def _step(provider: BaseProvider, model: str) -> PinnedResolver:
    return PinnedResolver(provider=provider, model=model)


# ---- constructor validation ----


def test_constructor_rejects_empty_steps() -> None:
    with pytest.raises(ValueError, match="at least one step"):
        FallbackResolver(steps=[])


def test_constructor_rejects_duplicate_steps() -> None:
    p = _stub_provider("minimax")
    with pytest.raises(ValueError, match="duplicate"):
        FallbackResolver(steps=[_step(p, "MiniMax-M3"), _step(p, "MiniMax-M3")])


# ---- happy path ----


def test_first_step_success_returns_immediately() -> None:
    primary = _stub_provider("minimax")
    backup = _stub_provider("anthropic")
    primary.chat.return_value = _response("minimax", "MiniMax-M3")
    r = FallbackResolver(steps=[_step(primary, "MiniMax-M3"), _step(backup, "claude-haiku-4-5")])

    resp = asyncio.run(r.ainvoke(_request()))

    assert resp.model == "MiniMax-M3"
    primary.chat.assert_awaited_once()
    backup.chat.assert_not_called()


# ---- trigger exceptions ----


def test_provider_unavailable_triggers_fallback() -> None:
    primary = _stub_provider("minimax")
    backup = _stub_provider("anthropic")
    primary.chat.side_effect = ProviderUnavailable("minimax 503")
    backup.chat.return_value = _response("anthropic", "claude-haiku-4-5")
    r = FallbackResolver(steps=[_step(primary, "MiniMax-M3"), _step(backup, "claude-haiku-4-5")])

    resp = asyncio.run(r.ainvoke(_request()))

    assert resp.provider_name == "anthropic"
    primary.chat.assert_awaited_once()
    backup.chat.assert_awaited_once()


def test_output_invalid_triggers_fallback() -> None:
    primary = _stub_provider("minimax")
    backup = _stub_provider("anthropic")
    primary.chat.side_effect = OutputInvalid("malformed JSON")
    backup.chat.return_value = _response("anthropic", "claude-haiku-4-5")
    r = FallbackResolver(steps=[_step(primary, "MiniMax-M3"), _step(backup, "claude-haiku-4-5")])

    resp = asyncio.run(r.ainvoke(_request()))

    assert resp.provider_name == "anthropic"


def test_rate_limited_triggers_fallback() -> None:
    primary = _stub_provider("minimax")
    backup = _stub_provider("anthropic")
    primary.chat.side_effect = RateLimited("429")
    backup.chat.return_value = _response("anthropic", "claude-haiku-4-5")
    r = FallbackResolver(steps=[_step(primary, "MiniMax-M3"), _step(backup, "claude-haiku-4-5")])

    resp = asyncio.run(r.ainvoke(_request()))

    assert resp.provider_name == "anthropic"


def test_timeout_triggers_fallback() -> None:
    primary = _stub_provider("minimax")
    backup = _stub_provider("anthropic")

    async def slow(*args, **kwargs):
        await asyncio.sleep(5)
        return _response("minimax", "MiniMax-M3")

    primary.chat.side_effect = slow
    backup.chat.return_value = _response("anthropic", "claude-haiku-4-5")
    r = FallbackResolver(
        steps=[_step(primary, "MiniMax-M3"), _step(backup, "claude-haiku-4-5")],
        attempt_timeout_s=0.05,
    )

    resp = asyncio.run(r.ainvoke(_request()))

    assert resp.provider_name == "anthropic"


# ---- non-trigger exception ----


def test_invalid_request_does_NOT_trigger_fallback() -> None:
    primary = _stub_provider("minimax")
    backup = _stub_provider("anthropic")
    primary.chat.side_effect = InvalidRequest("bad schema")
    r = FallbackResolver(steps=[_step(primary, "MiniMax-M3"), _step(backup, "claude-haiku-4-5")])

    with pytest.raises(InvalidRequest, match="bad schema"):
        asyncio.run(r.ainvoke(_request()))

    backup.chat.assert_not_called()


# ---- chain exhaustion ----


def test_all_steps_fail_raises_chain_exhausted() -> None:
    primary = _stub_provider("minimax")
    backup = _stub_provider("anthropic")
    primary.chat.side_effect = ProviderUnavailable("minimax 503")
    backup.chat.side_effect = RateLimited("anthropic 429")
    r = FallbackResolver(steps=[_step(primary, "MiniMax-M3"), _step(backup, "claude-haiku-4-5")])

    with pytest.raises(FallbackChainExhausted) as excinfo:
        asyncio.run(r.ainvoke(_request()))

    attempts = excinfo.value.attempts
    assert len(attempts) == 2
    assert attempts[0].provider_name == "minimax"
    assert attempts[0].model == "MiniMax-M3"
    assert attempts[0].exc_type == "ProviderUnavailable"
    assert attempts[1].provider_name == "anthropic"
    assert attempts[1].model == "claude-haiku-4-5"
    assert attempts[1].exc_type == "RateLimited"


def test_chain_exhausted_is_provider_unavailable() -> None:
    assert issubclass(FallbackChainExhausted, ProviderUnavailable)
