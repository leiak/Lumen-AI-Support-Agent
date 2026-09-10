"""Tests for the Anthropic provider adapter. HTTP calls mocked via pytest-httpx."""
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


async def test_anthropic_stream_not_implemented(provider: AnthropicProvider) -> None:
    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="Hi")],
    )
    with pytest.raises(NotImplementedError):
        async for _ in provider.stream(req):
            pass