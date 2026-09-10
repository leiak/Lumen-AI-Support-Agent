"""Integration tests for LLMClient (retry + usage recording). Requires live DB."""
import pytest
from pytest_httpx import HTTPXMock

from llm_client.client import LLMClient
from llm_client.exceptions import InvalidRequest, RateLimited
from llm_client.providers.anthropic_provider import AnthropicProvider
from llm_client.types import ChatMessage, ChatRequest, MessageRole


@pytest.fixture
def client_with_anthropic() -> LLMClient:
    p = AnthropicProvider(api_key="k", model="claude-3-5-sonnet-20241022")
    return LLMClient(default_provider=p, tenant_id="t-llm-client-test")


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
