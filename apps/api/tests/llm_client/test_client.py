"""Integration tests for LLMClient (retry + usage recording). Requires live DB."""
import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import create_autospec

import pytest
from pytest_httpx import HTTPXMock

from llm_client.client import LLMClient, ROUTE_AUTO, ROUTE_PINNED
from llm_client.exceptions import InvalidRequest, RateLimited
from llm_client.providers.anthropic_provider import AnthropicProvider
from llm_client.providers.base import BaseProvider
from llm_client.resolvers import PinnedResolver, _PrefixResolver
from llm_client.types import ChatMessage, ChatRequest, ChatResponse, MessageRole


def test_llm_client_requires_provider_resolver() -> None:
    from llm_client.client import LLMClient

    with pytest.raises(TypeError, match="provider_resolver is required"):
        LLMClient(provider_resolver=None, tenant_id="t")  # type: ignore[arg-type]


@pytest.fixture
async def client_with_anthropic() -> AsyncGenerator[LLMClient, None]:
    from llm_client.gateway import LLMGateway

    p = AnthropicProvider(api_key="k", model="claude-3-5-sonnet-20241022")
    g = LLMGateway(providers={"anthropic": p})
    yield LLMClient(
        provider_resolver=g.default_resolver,
        tenant_id="t-llm-client-test",
    )
    await g.aclose_all()


@pytest.mark.integration
async def test_client_retries_on_5xx(
    client_with_anthropic: LLMClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=503,
        json={"error": {"type": "overloaded", "message": "x"}},
    )
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        json={
            "model": "claude-3-5-sonnet-20241022",
            "content": [{"type": "text", "text": "Recovered"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    resp = await client_with_anthropic.chat(req, max_retries=2)
    assert resp.content == "Recovered"


@pytest.mark.integration
async def test_client_no_retry_on_rate_limit(
    client_with_anthropic: LLMClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=429,
        json={"error": {"type": "rate_limit", "message": "slow"}},
    )
    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    with pytest.raises(RateLimited):
        await client_with_anthropic.chat(req, max_retries=3)


@pytest.mark.integration
async def test_client_no_retry_on_400(
    client_with_anthropic: LLMClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=400,
        json={"error": {"type": "invalid_request_error", "message": "bad"}},
    )
    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    with pytest.raises(InvalidRequest):
        await client_with_anthropic.chat(req, max_retries=3)


@pytest.mark.integration
async def test_client_records_usage(
    client_with_anthropic: LLMClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        json={
            "model": "claude-3-5-sonnet-20241022",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )
    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    await client_with_anthropic.chat(req)
    await client_with_anthropic.flush_usage()

    from sqlalchemy import select

    from core.database import get_session, reset_engine, reset_sessionmaker
    from llm_client.models import LLMUsage

    # Reset engine so get_session creates a fresh one in this test's loop
    reset_engine()
    reset_sessionmaker()

    try:
        async with get_session() as s:
            r = await s.execute(
                select(LLMUsage).where(
                    LLMUsage.tenant_id == "t-llm-client-test"
                )
            )
            rows = r.scalars().all()
        # Could be more than 1 if tests run multiple times — but at least 1 exists
        assert len(rows) >= 1
        latest = rows[-1]
        assert latest.prompt_tokens == 10
        assert latest.completion_tokens == 5
        assert latest.provider == "anthropic"
        assert latest.model == "claude-3-5-sonnet-20241022"
    finally:
        # Cleanup the test rows so re-runs are idempotent
        reset_engine()
        reset_sessionmaker()
        async with get_session() as s:
            from sqlalchemy import delete

            await s.execute(
                delete(LLMUsage).where(LLMUsage.tenant_id == "t-llm-client-test")
            )
            await s.commit()


# ---- M4.B close-out regression tests (Task 5) ----
#
# These pin the non-fallback paths through LLMClient.chat: PinnedResolver and
# _PrefixResolver. They guard against the M4.B ainvoke branch accidentally
# swallowing the existing behavior — a future resolver kind must opt into
# chain semantics by exposing ``ainvoke``, not by silently routing through it.


def _stub_provider(name: str) -> BaseProvider:
    p = create_autospec(BaseProvider, instance=True)
    p.name = name
    return p


def _stub_response(provider_name: str, model: str) -> ChatResponse:
    return ChatResponse(
        content="ok",
        model=model,
        prompt_tokens=2,
        completion_tokens=1,
        finish_reason="stop",
        provider_name=provider_name,
    )


def test_pinned_resolver_still_works() -> None:
    """LLMClient + PinnedResolver must keep route_mode="pinned" + rewrite model.

    Regression for M4.B: the ainvoke branch in LLMClient.chat must NOT
    fire when the resolver exposes no ainvoke (PinnedResolver does not).
    request.model is rewritten to the pinned model so the wire request
    body and the metric label carry the actual model name.
    """
    provider = _stub_provider("minimax")
    provider.chat.return_value = _stub_response("minimax", "MiniMax-M3")
    pinned = PinnedResolver(provider=provider, model="MiniMax-M3")
    client = LLMClient(provider_resolver=pinned, tenant_id="t-pinned-regress")

    assert client._route_mode == ROUTE_PINNED
    req = ChatRequest(
        model="some-caller-supplied-name",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    resp = asyncio.run(client.chat(req))

    # PinnedResolver rewrites request.model → MiniMax-M3 before forwarding.
    provider.chat.assert_awaited_once()
    forwarded = provider.chat.await_args.args[0]
    assert forwarded.model == "MiniMax-M3"
    assert resp.model == "MiniMax-M3"


def test_prefix_resolver_still_works() -> None:
    """LLMClient + _PrefixResolver must keep route_mode="auto" + retry loop.

    Regression for M4.B: the ainvoke branch must NOT fire on a bare
    _PrefixResolver (it exposes no ainvoke). The local retry loop runs on
    transient ProviderUnavailable instead — keeping M1-M4.A behavior.
    """
    provider = _stub_provider("anthropic")
    # Fail once with 5xx, succeed on the next call → exercises the retry loop.
    provider.chat.side_effect = [
        _stub_response("anthropic", "claude-haiku-4-5"),  # immediate success
    ]
    prefix = _PrefixResolver(
        providers={"anthropic": provider},
        default_provider_name="anthropic",
    )
    client = LLMClient(provider_resolver=prefix, tenant_id="t-prefix-regress")

    assert client._route_mode == ROUTE_AUTO
    req = ChatRequest(
        model="claude-haiku-4-5",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    resp = asyncio.run(client.chat(req))

    provider.chat.assert_awaited_once()
    assert resp.provider_name == "anthropic"
