"""Tests for the Anthropic provider adapter. HTTP calls mocked via pytest-httpx."""
import json

import httpx
import pytest
from pytest_httpx import HTTPXMock

from llm_client.exceptions import (
    InvalidRequest,
    OutputInvalid,
    ProviderUnavailable,
    RateLimited,
)
from llm_client.providers.anthropic_provider import AnthropicProvider
from llm_client.types import ChatMessage, ChatRequest, MessageRole


@pytest.fixture
def provider() -> AnthropicProvider:
    return AnthropicProvider(api_key="test-key", model="claude-3-5-sonnet-20241022")


async def test_anthropic_chat_success(provider: AnthropicProvider, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        json={
            "id": "msg_01",
            "model": "claude-3-5-sonnet-20241022",
            "content": [{"type": "text", "text": "Hello back"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 8},
        },
    )

    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="Hello")],
    )
    resp = await provider.chat(req)
    assert resp.content == "Hello back"
    assert resp.prompt_tokens == 12
    assert resp.completion_tokens == 8
    assert resp.finish_reason == "stop"
    assert resp.total_tokens == 20


async def test_anthropic_chat_rate_limited(
    provider: AnthropicProvider, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=429,
        json={"error": {"type": "rate_limit_error", "message": "Too many requests"}},
    )

    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="Hi")],
    )
    with pytest.raises(RateLimited):
        await provider.chat(req)


async def test_anthropic_chat_400_invalid_request(
    provider: AnthropicProvider, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=400,
        json={"error": {"type": "invalid_request_error", "message": "bad model"}},
    )

    req = ChatRequest(
        model="bad-model",
        messages=[ChatMessage(role=MessageRole.USER, content="Hi")],
    )
    with pytest.raises(InvalidRequest):
        await provider.chat(req)


async def test_anthropic_chat_500_unavailable(
    provider: AnthropicProvider, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=500,
        text="Internal Server Error",
    )

    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="Hi")],
    )
    with pytest.raises(ProviderUnavailable):
        await provider.chat(req)


async def test_anthropic_chat_network_error(
    provider: AnthropicProvider, httpx_mock: HTTPXMock
) -> None:
    """Network failure (timeout, connection refused) should map to ProviderUnavailable."""
    httpx_mock.add_exception(httpx.ConnectError("connection refused"))

    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="Hi")],
    )
    with pytest.raises(ProviderUnavailable):
        await provider.chat(req)


async def test_anthropic_chat_invalid_json(
    provider: AnthropicProvider, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=200,
        text="not json",
    )

    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="Hi")],
    )
    with pytest.raises(OutputInvalid):
        await provider.chat(req)


async def test_anthropic_system_message_extracted(
    provider: AnthropicProvider, httpx_mock: HTTPXMock
) -> None:
    """Anthropic API takes 'system' as a top-level field, not in messages. Verify translation."""
    import json

    captured: dict = {}

    def callback(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_01",
                "model": "claude-3-5-sonnet-20241022",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        )

    httpx_mock.add_callback(callback)

    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[
            ChatMessage(role=MessageRole.SYSTEM, content="You are helpful"),
            ChatMessage(role=MessageRole.USER, content="Hi"),
        ],
    )
    await provider.chat(req)

    body = captured["body"]
    assert body["system"] == "You are helpful"
    # system message removed from messages array
    assert all(m["role"] != "system" for m in body["messages"])
    assert body["messages"][0]["role"] == "user"


def test_anthropic_empty_api_key_rejected() -> None:
    with pytest.raises(ValueError, match="API key"):
        AnthropicProvider(api_key="", model="claude-3-5-sonnet-20241022")


async def test_anthropic_chat_uses_custom_base_url(httpx_mock: HTTPXMock) -> None:
    """Reroute the Anthropic-format POST to a proxy base (e.g. MiniMax
    Anthropic-compatible endpoint at https://api.minimaxi.com/anthropic).

    Regression for M4.C follow-up: real Anthropic keys hit api.anthropic.com,
    but third-party proxies (which Claude Code itself uses) need a custom
    base URL. The provider must honour ``base_url=`` and POST ``/v1/messages``
    under it without code changes.
    """
    proxy = AnthropicProvider(
        api_key="proxy-key",
        model="MiniMax-M3",
        base_url="https://api.minimaxi.com/anthropic",
    )
    httpx_mock.add_response(
        url="https://api.minimaxi.com/anthropic/v1/messages",
        json={
            "id": "msg_proxy",
            "model": "MiniMax-M3",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )

    req = ChatRequest(
        model="MiniMax-M3",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    resp = await proxy.chat(req)
    assert resp.content == "ok"
    assert resp.model == "MiniMax-M3"


async def test_anthropic_default_base_url_unchanged(httpx_mock: HTTPXMock) -> None:
    """When ``base_url`` is not passed the provider must still hit
    ``https://api.anthropic.com/v1/messages`` — protects the existing
    real-Anthropic-key callers from the new optional arg breaking them.
    """
    p = AnthropicProvider(api_key="k", model="claude-3-5-sonnet-20241022")
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        json={
            "id": "msg_1",
            "model": "claude-3-5-sonnet-20241022",
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    resp = await p.chat(req)
    assert resp.content == "hi"


def _sse(payloads):
    """Join event dicts into an SSE body: 'data: <json>\n' per event."""
    nl = chr(10)
    parts = [pl if isinstance(pl, str) else json.dumps(pl) for pl in payloads]
    return nl.join("data: " + part for part in parts) + nl


async def test_anthropic_stream_success(httpx_mock: HTTPXMock) -> None:
    provider = AnthropicProvider(
        api_key="test-key", model="claude-3-5-sonnet-20241022"
    )
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        text=_sse(
            [
                {
                    "type": "message_start",
                    "message": {
                        "model": "claude-3-5-sonnet-20241022",
                        "usage": {"input_tokens": 12},
                    },
                },
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 8},
                },
                {"type": "message_stop"},
            ]
        ),
        headers={"Content-Type": "text/event-stream"},
    )
    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="Hi")],
    )
    items = [item async for item in provider.stream(req)]
    deltas = [i for i in items if isinstance(i, str)]
    assert deltas == ["Hel", "lo"]
    final = items[-1]
    assert final.content == "Hello"
    assert final.prompt_tokens == 12
    assert final.completion_tokens == 8
    assert final.finish_reason == "stop"


async def test_anthropic_stream_rate_limited(httpx_mock: HTTPXMock) -> None:
    provider = AnthropicProvider(
        api_key="test-key", model="claude-3-5-sonnet-20241022"
    )
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=429,
        json={"error": {"type": "rate_limit_error", "message": "slow"}},
    )
    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="Hi")],
    )
    with pytest.raises(RateLimited):
        async for _item in provider.stream(req):
            pass
