"""Unit tests for LLMClient.stream_chat using a fake provider (no DB/network).

Streaming cannot run against a live provider in unit tests, so we drive the
async-generator protocol with a lightweight in-memory provider and verify both
the delta forwarding and the usage-accounting behaviour on the final
ChatResponse.
"""
from collections.abc import AsyncIterator

from llm_client.client import LLMClient
from llm_client.providers.base import BaseProvider
from llm_client.types import ChatMessage, ChatRequest, ChatResponse, MessageRole


class _FakeStreamProvider(BaseProvider):
    """Yields two text deltas then a final ChatResponse with usage."""

    name = "fake"

    async def chat(self, request: ChatRequest) -> ChatResponse:
        raise AssertionError("stream_chat must not call chat()")

    async def stream(
        self, request: ChatRequest
    ) -> AsyncIterator["ChatResponse | str"]:
        yield "one"
        yield "two"
        yield ChatResponse(
            content="onetwo",
            model="fake-model",
            prompt_tokens=3,
            completion_tokens=2,
            finish_reason="stop",
        )


def _make_request() -> ChatRequest:
    return ChatRequest(
        model="fake-model",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )


async def test_stream_chat_forwards_deltas_then_final_response() -> None:
    client = LLMClient(default_provider=_FakeStreamProvider(), tenant_id="t-1")
    items = [item async for item in client.stream_chat(_make_request())]

    assert [i for i in items if isinstance(i, str)] == ["one", "two"]
    assert isinstance(items[-1], ChatResponse)
    assert items[-1].content == "onetwo"
    assert items[-1].finish_reason == "stop"


async def test_stream_chat_records_usage_on_final_response() -> None:
    client = LLMClient(default_provider=_FakeStreamProvider(), tenant_id="t-1")
    items = [item async for item in client.stream_chat(_make_request())]
    # Collect to completion ensures the final sentinel was consumed.
    assert isinstance(items[-1], ChatResponse)

    assert len(client.usage._pending) == 1
    row = client.usage._pending[0]
    assert row["tenant_id"] == "t-1"
    assert row["provider"] == "fake"
    assert row["prompt_tokens"] == 3
    assert row["completion_tokens"] == 2
