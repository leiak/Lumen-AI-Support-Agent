"""Unit tests for LLMClient ainvoke branch + max_retries default."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, create_autospec

import pytest

from llm_client.client import ROUTE_FALLBACK, LLMClient
from llm_client.exceptions import (
    FallbackChainExhausted,
    ProviderUnavailable,
)
from llm_client.providers.base import BaseProvider
from llm_client.resolvers import FallbackResolver, PinnedResolver
from llm_client.types import ChatMessage, ChatRequest, ChatResponse, MessageRole


def _stub_provider(name: str) -> BaseProvider:
    p = create_autospec(BaseProvider, instance=True)
    p.name = name
    return p


def _request() -> ChatRequest:
    return ChatRequest(
        model="MiniMax-M3",
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


# ---- route_mode label ----


def test_route_mode_for_fallback_resolver() -> None:
    p = _stub_provider("minimax")
    r = FallbackResolver(steps=[PinnedResolver(provider=p, model="MiniMax-M3")])
    client = LLMClient(provider_resolver=r, tenant_id="t1")
    assert client._route_mode == ROUTE_FALLBACK


# ---- ainvoke branch ----


def test_chat_uses_ainvoke_when_resolver_exposes_it() -> None:
    primary = _stub_provider("minimax")
    ainvoke_mock = AsyncMock(return_value=_response("minimax", "MiniMax-M3"))
    real = FallbackResolver(steps=[PinnedResolver(provider=primary, model="MiniMax-M3")])
    real.ainvoke = ainvoke_mock  # type: ignore[method-assign]
    client = LLMClient(provider_resolver=real, tenant_id="t1")

    resp = asyncio.run(client.chat(_request()))

    ainvoke_mock.assert_awaited_once()
    assert resp.provider_name == "minimax"
    # Primary provider.chat was NOT called directly — the chain ran via ainvoke.
    primary.chat.assert_not_called()


def test_chat_propagates_chain_exhausted_without_internal_retry() -> None:
    primary = _stub_provider("minimax")
    real = FallbackResolver(steps=[PinnedResolver(provider=primary, model="MiniMax-M3")])
    real.ainvoke = AsyncMock(  # type: ignore[method-assign]
        side_effect=FallbackChainExhausted(attempts=[])
    )
    client = LLMClient(provider_resolver=real, tenant_id="t1")

    with pytest.raises(FallbackChainExhausted):
        asyncio.run(client.chat(_request()))


def test_chat_does_not_retry_chain_exhausted_even_with_max_retries_5() -> None:
    """LLMClient.chat's retry loop must NOT run when the resolver has ainvoke.

    Otherwise max_retries=5 * chain_length=2 = up to 10 attempts.  # noqa: RUF002
    """
    primary = _stub_provider("minimax")
    real = FallbackResolver(steps=[PinnedResolver(provider=primary, model="MiniMax-M3")])
    call_count = 0

    async def fake_ainvoke(req):
        nonlocal call_count
        call_count += 1
        raise FallbackChainExhausted(attempts=[])

    real.ainvoke = fake_ainvoke  # type: ignore[method-assign]
    client = LLMClient(provider_resolver=real, tenant_id="t1")

    with pytest.raises(FallbackChainExhausted):
        asyncio.run(client.chat(_request(), max_retries=5))

    assert call_count == 1, f"expected 1 ainvoke call, got {call_count}"


# ---- max_retries default ----


def test_default_max_retries_is_one_for_non_fallback_resolver() -> None:
    """PinnedResolver / PrefixResolver path now defaults to 1 (was 3)."""
    p = _stub_provider("minimax")
    p.chat.side_effect = ProviderUnavailable("503")
    pinned = PinnedResolver(provider=p, model="MiniMax-M3")
    client = LLMClient(provider_resolver=pinned, tenant_id="t1")

    with pytest.raises(ProviderUnavailable):
        asyncio.run(client.chat(_request()))

    # max_retries=1 → exactly 2 attempts (initial + 1 retry).
    assert p.chat.await_count == 2, f"expected 2 attempts, got {p.chat.await_count}"
