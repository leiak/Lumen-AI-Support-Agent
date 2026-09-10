"""Tests for the OpenAI provider adapter and OllamaProvider subclass."""
import httpx
import pytest
from pytest_httpx import HTTPXMock

from llm_client.exceptions import (
    InvalidRequest,
    OutputInvalid,
    ProviderUnavailable,
    RateLimited,
)
from llm_client.providers.ollama_provider import OllamaProvider
from llm_client.providers.openai_provider import OpenAIProvider
from llm_client.types import ChatMessage, ChatRequest, MessageRole


async def test_openai_chat_success(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.openai.com/v1/chat/completions",
        json={
            "id": "cmpl-1",
            "model": "gpt-4o",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                    "index": 0,
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        },
    )

    p = OpenAIProvider(api_key="k", model="gpt-4o", base_url="https://api.openai.com/v1")
    req = ChatRequest(
        model="gpt-4o",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    resp = await p.chat(req)
    assert resp.content == "ok"
    assert resp.total_tokens == 8
    assert resp.prompt_tokens == 5
    assert resp.completion_tokens == 3
    assert resp.finish_reason == "stop"


async def test_openai_chat_rate_limited(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.openai.com/v1/chat/completions",
        status_code=429,
        text="Too Many Requests",
    )
    p = OpenAIProvider(api_key="k", model="gpt-4o", base_url="https://api.openai.com/v1")
    req = ChatRequest(
        model="gpt-4o",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    with pytest.raises(RateLimited):
        await p.chat(req)


async def test_openai_chat_400_invalid(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.openai.com/v1/chat/completions",
        status_code=400,
        json={"error": {"message": "bad model"}},
    )
    p = OpenAIProvider(api_key="k", model="gpt-4o", base_url="https://api.openai.com/v1")
    req = ChatRequest(
        model="gpt-4o",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    with pytest.raises(InvalidRequest):
        await p.chat(req)


async def test_openai_chat_500_unavailable(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.openai.com/v1/chat/completions",
        status_code=500,
        text="Internal Server Error",
    )
    p = OpenAIProvider(api_key="k", model="gpt-4o", base_url="https://api.openai.com/v1")
    req = ChatRequest(
        model="gpt-4o",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    with pytest.raises(ProviderUnavailable):
        await p.chat(req)


async def test_openai_chat_network_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    p = OpenAIProvider(api_key="k", model="gpt-4o", base_url="https://api.openai.com/v1")
    req = ChatRequest(
        model="gpt-4o",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    with pytest.raises(ProviderUnavailable):
        await p.chat(req)


async def test_openai_chat_invalid_json(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.openai.com/v1/chat/completions",
        status_code=200,
        text="not json",
    )
    p = OpenAIProvider(api_key="k", model="gpt-4o", base_url="https://api.openai.com/v1")
    req = ChatRequest(
        model="gpt-4o",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    with pytest.raises(OutputInvalid):
        await p.chat(req)


async def test_ollama_provider_no_api_key_required(httpx_mock: HTTPXMock) -> None:
    """OllamaProvider should accept empty api_key (Ollama doesn't require auth)."""
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        json={
            "id": "ollama-1",
            "model": "llama3",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "from-ollama"},
                    "finish_reason": "stop",
                    "index": 0,
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )

    p = OllamaProvider(base_url="http://localhost:11434/v1", model="llama3")
    assert p.name == "ollama"  # subclass overrides the name
    req = ChatRequest(
        model="llama3",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    resp = await p.chat(req)
    assert resp.content == "from-ollama"


def test_openai_empty_api_key_for_openai_com_rejected() -> None:
    """OpenAI base URL with empty api_key is rejected."""
    with pytest.raises(ValueError, match="API key"):
        OpenAIProvider(api_key="", model="gpt-4o", base_url="https://api.openai.com/v1")


def test_openai_empty_api_key_for_other_base_url_allowed() -> None:
    """Non-OpenAI base URL (e.g. local proxy) with empty api_key is allowed."""
    p = OpenAIProvider(api_key="", model="x", base_url="http://localhost:1234/v1")
    assert p.api_key == ""
