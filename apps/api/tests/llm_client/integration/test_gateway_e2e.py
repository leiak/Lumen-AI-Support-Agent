"""End-to-end integration tests for the LLM gateway via mocked HTTP.

These tests exercise the full ``LLMClient -> gateway resolver ->
BaseProvider`` path against mocked provider APIs (``pytest-httpx``).

Goals:
- Verify the prefix-based auto-router dispatches by ``request.model``
  prefix to the correct registered provider (MiniMax via
  ``OpenAIProvider`` with ``api.minimaxi.com`` base URL; Anthropic via
  ``AnthropicProvider`` with ``api.anthropic.com``).
- Verify the ``gateway.with_config(provider=..., model=...)``
  ``PinnedResolver`` path ignores the caller's ``request.model`` and
  rewrites it to the pinned model before the provider sees it.
- Verify an unknown model name (when no default fallback is
  registered) raises ``InvalidRequest`` without making any HTTP call.

PII discipline: the test messages and responses are synthetic placeholders
(``"hi"`` / ``"Hello!"``) — never real customer data.

Marked ``@pytest.mark.integration`` so the default selector
(``pytest -m "not integration"``) skips them, matching the existing
``test_client.py`` convention.
"""
import json
from collections.abc import AsyncGenerator

import pytest
from pytest_httpx import HTTPXMock

from llm_client.client import LLMClient
from llm_client.exceptions import InvalidRequest
from llm_client.gateway import LLMGateway
from llm_client.providers.anthropic_provider import AnthropicProvider
from llm_client.providers.openai_provider import OpenAIProvider
from llm_client.types import ChatMessage, ChatRequest, MessageRole


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
async def anthropic_only_gateway() -> AsyncGenerator[LLMGateway, None]:
    """Gateway with a single Anthropic provider; used for the prefix-routed
    Anthropic test and the unknown-model fallback test."""
    p = AnthropicProvider(api_key="test-key", model="claude-haiku-4-5")
    g = LLMGateway(providers={"anthropic": p})
    try:
        yield g
    finally:
        await g.aclose_all()


@pytest.fixture
async def minimax_plus_anthropic_gateway() -> AsyncGenerator[LLMGateway, None]:
    """Gateway with both providers registered; used for prefix-routed
    MiniMax + the pinned-resolver test."""
    mm = OpenAIProvider(
        api_key="test-key",
        model="MiniMax-M3",
        base_url="https://api.minimaxi.com/v1",
    )
    ant = AnthropicProvider(api_key="test-key", model="claude-haiku-4-5")
    g = LLMGateway(providers={"minimax": mm, "anthropic": ant})
    try:
        yield g
    finally:
        await g.aclose_all()


# --- Tests ------------------------------------------------------------------


@pytest.mark.integration
async def test_minimax_chat_succeeds_via_resolver(
    minimax_plus_anthropic_gateway: LLMGateway, httpx_mock: HTTPXMock
) -> None:
    """A request with model ``MiniMax-M3`` (matches the ``minimax-`` prefix)
    routes through the default resolver to ``OpenAIProvider`` and hits the
    MiniMax base URL."""
    client = LLMClient(
        provider_resolver=minimax_plus_anthropic_gateway.default_resolver,
        tenant_id="t-minimax-e2e",
    )
    httpx_mock.add_response(
        url="https://api.minimaxi.com/v1/chat/completions",
        json={
            "id": "chatcmpl-minimax-1",
            "model": "MiniMax-M3",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )

    req = ChatRequest(
        model="MiniMax-M3",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    try:
        resp = await client.chat(req)
    finally:
        await client.aclose()

    assert resp.content == "Hello!"
    assert resp.model == "MiniMax-M3"
    assert resp.prompt_tokens == 10
    assert resp.completion_tokens == 5
    assert resp.finish_reason == "stop"

    # The mock recorded exactly one request, and it landed on MiniMax.
    recorded = httpx_mock.get_requests()
    assert len(recorded) == 1, f"expected 1 HTTP call, got {len(recorded)}"
    assert recorded[0].url.host == "api.minimaxi.com"
    assert recorded[0].url.path == "/v1/chat/completions"


@pytest.mark.integration
async def test_anthropic_chat_succeeds_via_resolver(
    anthropic_only_gateway: LLMGateway, httpx_mock: HTTPXMock
) -> None:
    """A request with model ``claude-...`` matches the ``claude-`` prefix
    and routes to ``AnthropicProvider``."""
    client = LLMClient(
        provider_resolver=anthropic_only_gateway.default_resolver,
        tenant_id="t-anthropic-e2e",
    )
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        json={
            "id": "msg_anthropic_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": "Hello!"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )

    req = ChatRequest(
        model="claude-haiku-4-5",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    try:
        resp = await client.chat(req)
    finally:
        await client.aclose()

    assert resp.content == "Hello!"
    assert resp.model == "claude-haiku-4-5"
    assert resp.prompt_tokens == 10
    assert resp.completion_tokens == 5
    assert resp.finish_reason == "stop"

    recorded = httpx_mock.get_requests()
    assert len(recorded) == 1, f"expected 1 HTTP call, got {len(recorded)}"
    assert recorded[0].url.host == "api.anthropic.com"
    assert recorded[0].url.path == "/v1/messages"


@pytest.mark.integration
async def test_qa_judge_pinned_path_routes_to_minimax(
    minimax_plus_anthropic_gateway: LLMGateway, httpx_mock: HTTPXMock
) -> None:
    """``gateway.with_config(provider="minimax", model="MiniMax-M3")``
    pins the call regardless of ``request.model``. Even when the caller
    passes ``model="claude-haiku-4-5"`` (which would otherwise route to
    Anthropic), the pinned resolver forces MiniMax — and ``LLMClient``
    rewrites ``request.model`` to the pinned model so the provider's
    request body actually carries ``"MiniMax-M3"``.
    """
    pinned = minimax_plus_anthropic_gateway.with_config(
        provider="minimax", model="MiniMax-M3"
    )
    client = LLMClient(provider_resolver=pinned, tenant_id="t-qa-judge-e2e")
    httpx_mock.add_response(
        url="https://api.minimaxi.com/v1/chat/completions",
        json={
            "id": "chatcmpl-pinned-1",
            "model": "MiniMax-M3",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "scored"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )

    req = ChatRequest(
        model="claude-haiku-4-5",  # caller-supplied, ignored by pinned resolver
        messages=[ChatMessage(role=MessageRole.USER, content="score this")],
    )
    try:
        resp = await client.chat(req)
    finally:
        await client.aclose()

    assert resp.model == "MiniMax-M3"
    assert resp.content == "scored"

    recorded = httpx_mock.get_requests()
    assert len(recorded) == 1, f"expected 1 HTTP call, got {len(recorded)}"
    assert recorded[0].url.host == "api.minimaxi.com"
    assert recorded[0].url.path == "/v1/chat/completions"

    # The provider's outbound request body MUST carry the pinned model,
    # not the caller-supplied "claude-haiku-4-5". This is the whole point
    # of the PinnedResolver — override the model name on the wire.
    body = json.loads(recorded[0].content)
    assert body["model"] == "MiniMax-M3", (
        f"PinnedResolver should have rewritten model to MiniMax-M3; "
        f"got {body['model']!r}"
    )


@pytest.mark.integration
async def test_default_routing_unknown_model_returns_invalid_request(
    httpx_mock: HTTPXMock,
) -> None:
    """When the prefix resolver cannot match the model's family AND the
    default provider is unavailable, ``LLMClient`` catches the
    ``UnknownModelError`` and re-raises as ``InvalidRequest``. No HTTP
    call should have been made — the resolver fails before any
    provider is asked.

    We construct a fresh gateway here with a ``default_provider_name``
    that is NOT in the registry to force the ``UnknownModelError``
    branch on the prefix resolver.
    """
    g = LLMGateway(
        providers={
            "anthropic": AnthropicProvider(
                api_key="test-key", model="claude-haiku-4-5"
            ),
        },
        default_provider_name="openai",  # not registered → fallback path fails
    )
    client = LLMClient(provider_resolver=g.default_resolver, tenant_id="t-unknown-model")

    req = ChatRequest(
        model="mystery-model-xyz",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    try:
        with pytest.raises(InvalidRequest, match="No LLM provider registered"):
            await client.chat(req)
    finally:
        await client.aclose()
        await g.aclose_all()

    # Zero HTTP calls — the resolver failed before any provider was asked.
    assert len(httpx_mock.get_requests()) == 0