"""End-to-end tests for FallbackResolver chain via pytest-httpx.

These tests spin up a real LLMClient backed by httpx-mocked provider
responses, exercising the full chain path: gateway → FallbackResolver
ainvoke → provider.chat → http → back.
"""
from __future__ import annotations

import asyncio

import pytest
from pytest_httpx import HTTPXMock

from llm_client.client import LLMClient
from llm_client.exceptions import (
    FallbackChainExhausted,
    ProviderUnavailable,
)
from llm_client.gateway import LLMGateway
from llm_client.providers.anthropic_provider import AnthropicProvider
from llm_client.providers.openai_provider import OpenAIProvider
from llm_client.types import ChatMessage, ChatRequest, MessageRole


def _request() -> ChatRequest:
    return ChatRequest(
        model="MiniMax-M3",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )


def _build_gateway_with_chain(
    *, attempt_timeout_s: float | None = None
) -> LLMGateway:
    providers = {
        "minimax": OpenAIProvider(
            api_key="test-key",
            model="MiniMax-M3",
            base_url="https://api.minimaxi.com/v1",
        ),
        "anthropic": AnthropicProvider(
            api_key="test-key",
            model="claude-haiku-4-5",
        ),
    }
    return LLMGateway(
        providers=providers,
        default_fallback_chain=[
            ("minimax", "MiniMax-M3"),
            ("anthropic", "claude-haiku-4-5"),
        ],
        attempt_timeout_s=attempt_timeout_s,
    )


def test_e2e_primary_5xx_falls_back(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.minimaxi.com/v1/chat/completions",
        status_code=503,
    )
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": "fallback ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        },
    )

    gateway = _build_gateway_with_chain()
    client = LLMClient(provider_resolver=gateway.default_resolver, tenant_id="t1")
    resp = asyncio.run(client.chat(_request()))

    assert resp.model == "claude-haiku-4-5"
    # Both providers were hit exactly once.
    assert len(httpx_mock.get_requests()) == 2


def test_e2e_primary_timeout_falls_back(httpx_mock: HTTPXMock) -> None:
    # minimax hangs past attempt_timeout_s; anthropic returns 200.
    async def slow_minimax(request) -> None:
        await asyncio.sleep(2.0)
        return None  # never reached — wait_for fires first

    httpx_mock.add_callback(slow_minimax, url="https://api.minimaxi.com/v1/chat/completions")
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": "fallback ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        },
    )

    gateway = _build_gateway_with_chain(attempt_timeout_s=0.1)
    client = LLMClient(provider_resolver=gateway.default_resolver, tenant_id="t1")
    resp = asyncio.run(client.chat(_request()))

    assert resp.model == "claude-haiku-4-5"


def test_e2e_all_5xx_raises_chain_exhausted(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.minimaxi.com/v1/chat/completions",
        status_code=503,
    )
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=502,
    )

    gateway = _build_gateway_with_chain()
    client = LLMClient(provider_resolver=gateway.default_resolver, tenant_id="t1")
    with pytest.raises(FallbackChainExhausted) as excinfo:
        asyncio.run(client.chat(_request()))
    assert len(excinfo.value.attempts) == 2


def test_e2e_stream_chat_skips_fallback(httpx_mock: HTTPXMock) -> None:
    """stream_chat() uses the resolver's __call__ (primary only), not ainvoke.

    A 5xx on primary must NOT trigger backup — verify backup is not hit.
    """
    httpx_mock.add_response(
        url="https://api.minimaxi.com/v1/chat/completions",
        status_code=503,
    )

    gateway = _build_gateway_with_chain()
    client = LLMClient(provider_resolver=gateway.default_resolver, tenant_id="t1")

    async def drain():
        async for _ in client.stream_chat(_request()):
            pass

    with pytest.raises(ProviderUnavailable):
        asyncio.run(drain())

    # Only minimax was hit — anthropic was NOT consulted.
    assert len(httpx_mock.get_requests()) == 1
