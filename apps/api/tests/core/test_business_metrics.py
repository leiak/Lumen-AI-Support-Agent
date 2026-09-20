"""Stage 11.3: business metric wiring tests.

Each test snapshots a counter's value before the operation, performs the
operation, then asserts the value changed by exactly the expected amount
with the expected labels. We use the module-level Prometheus counters
directly — they're process-global by design, so no DI gymnastics.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.business_metrics import (
    LLM_CALLS_TOTAL,
    LLM_TOKENS_TOTAL,
    MESSAGES_TOTAL,
)
from conversation.enums import MessageRole as ConvMessageRole
from conversation.repository import ConversationRepository, MessageRepository
from conversation.service import ConversationService
from llm_client.client import LLMClient
from llm_client.exceptions import ProviderUnavailable, RateLimited
from llm_client.gateway import LLMGateway
from llm_client.providers.base import BaseProvider
from llm_client.types import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    MessageRole,
)


def _counter_value(metric: Any, **labels: str) -> float:
    """Read the current value of a labelled counter.

    Prometheus stores child counters by their label tuple; we read via
    ``_metrics`` to avoid depending on the public sample API.
    """
    if labels:
        child = metric.labels(**labels)
    else:
        # Unlabelled metric (none in this module, but symmetric).
        child = metric
    return child._value.get()  # type: ignore[attr-defined]  # noqa: SLF001


# ---- Shared fixture: factory that wraps a fake provider in a gateway
#      and yields an LLMClient, cleaning up the gateway on teardown.
#
# Mirrors the pattern in ``tests/llm_client/test_client.py``'s
# ``client_with_anthropic`` fixture, but parameterised over the
# provider so each test can pass its own FakeProvider / RateLimited /
# Flaky implementation inline.


@pytest.fixture
async def make_llm_client() -> AsyncGenerator[Callable[..., LLMClient], None]:
    """Return a function: ``(provider, tenant_id) -> LLMClient``.

    Tracks every gateway created so ``aclose_all`` runs on teardown,
    even when fake providers don't expose ``aclose`` (defensive: keeps
    the pattern consistent with production gateways).
    """
    gateways: list[LLMGateway] = []

    def _factory(
        provider: BaseProvider, tenant_id: str = "t1"
    ) -> LLMClient:
        g = LLMGateway(providers={provider.name: provider})
        gateways.append(g)
        return LLMClient(
            provider_resolver=g.default_resolver,
            tenant_id=tenant_id,
        )

    yield _factory
    for g in gateways:
        await g.aclose_all()


# ---- MESSAGES_TOTAL via ConversationService ----


@pytest.mark.asyncio
async def test_messages_total_increments_per_role() -> None:
    """``ConversationService.record_message`` bumps the counter per role.

    We mock the repositories so the test doesn't need a live DB. The
    service's metric side-effect is what we're verifying.
    """
    conv_repo = MagicMock(spec=ConversationRepository)
    msg_repo = MagicMock(spec=MessageRepository)
    # ``ConversationService.get`` calls ``self._repo.get_by_id(conversation_id)``
    # then asserts ``conv.tenant_id == tenant_id``; we satisfy both by
    # mocking get_by_id to return a sentinel whose tenant_id matches.
    sentinel = MagicMock()
    sentinel.id = "conv-1"
    sentinel.tenant_id = "t1"
    conv_repo.get_by_id = AsyncMock(return_value=sentinel)
    conv_repo.touch_last_activity = AsyncMock()
    persisted_msg = MagicMock()
    persisted_msg.id = "msg-1"
    msg_repo.create = AsyncMock(return_value=persisted_msg)

    svc = ConversationService(repo=conv_repo, message_repo=msg_repo)

    for role in (ConvMessageRole.CUSTOMER, ConvMessageRole.AI, ConvMessageRole.AGENT):
        before = _counter_value(MESSAGES_TOTAL, role=role.value)
        await svc.record_message(
            tenant_id="t1",
            conversation_id="conv-1",
            role=role,
            content_text="hi",
        )
        after = _counter_value(MESSAGES_TOTAL, role=role.value)
        assert after == before + 1, f"{role.value} did not increment"


# ---- LLM_CALLS_TOTAL / LLM_TOKENS_TOTAL via LLMClient.chat ----


@pytest.mark.asyncio
async def test_llm_calls_success_increments_calls_and_tokens(
    make_llm_client: Callable[..., LLMClient],
) -> None:

    class FakeProvider(BaseProvider):
        name = "fake-provider"

        async def chat(self, request: ChatRequest) -> ChatResponse:
            return ChatResponse(
                content="hi",
                model="fake-model-1",
                prompt_tokens=10,
                completion_tokens=20,
                finish_reason="stop",
            )

        async def stream(self, request: ChatRequest):
            yield ChatResponse(
                content="hi",
                model="fake-model-1",
                prompt_tokens=10,
                completion_tokens=20,
                finish_reason="stop",
            )

    client = make_llm_client(FakeProvider())

    before_calls = _counter_value(
        LLM_CALLS_TOTAL,
        provider="fake-provider",
        model="fake-model-1",
        route_mode="auto",
        outcome="success",
    )
    before_in = _counter_value(
        LLM_TOKENS_TOTAL,
        provider="fake-provider",
        model="fake-model-1",
        route_mode="auto",
        direction="input",
    )
    before_out = _counter_value(
        LLM_TOKENS_TOTAL,
        provider="fake-provider",
        model="fake-model-1",
        route_mode="auto",
        direction="output",
    )

    resp = await client.chat(
        ChatRequest(
            model="fake-model-1",
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
        )
    )
    assert resp.content == "hi"

    assert _counter_value(
        LLM_CALLS_TOTAL,
        provider="fake-provider",
        model="fake-model-1",
        route_mode="auto",
        outcome="success",
    ) == before_calls + 1
    assert _counter_value(
        LLM_TOKENS_TOTAL,
        provider="fake-provider",
        model="fake-model-1",
        route_mode="auto",
        direction="input",
    ) == before_in + 10
    assert _counter_value(
        LLM_TOKENS_TOTAL,
        provider="fake-provider",
        model="fake-model-1",
        route_mode="auto",
        direction="output",
    ) == before_out + 20


@pytest.mark.asyncio
async def test_llm_calls_rate_limited_outcome(
    make_llm_client: Callable[..., LLMClient],
) -> None:

    class RateLimitedProvider(BaseProvider):
        name = "rl-provider"

        async def chat(self, request: ChatRequest) -> None:
            raise RateLimited("nope")

        async def stream(self, request: ChatRequest):
            if False:
                yield None
            raise RateLimited("nope")

    client = make_llm_client(RateLimitedProvider())
    before = _counter_value(
        LLM_CALLS_TOTAL,
        provider="<unknown>",
        model="rl-model",
        route_mode="auto",
        outcome="rate_limited",
    )

    with pytest.raises(RateLimited):
        await client.chat(
            ChatRequest(
                messages=[ChatMessage(role=MessageRole.USER, content="hi")],
                model="rl-model",
            )
        )

    assert _counter_value(
        LLM_CALLS_TOTAL,
        provider="<unknown>",
        model="rl-model",
        route_mode="auto",
        outcome="rate_limited",
    ) == before + 1


@pytest.mark.asyncio
async def test_llm_calls_unavailable_retried_then_succeeds(
    make_llm_client: Callable[..., LLMClient],
) -> None:

    class FlakyProvider(BaseProvider):
        name = "flaky"
        attempts = 0

        async def chat(self, request: ChatRequest) -> ChatResponse:
            self.attempts += 1
            if self.attempts < 2:
                raise ProviderUnavailable("5xx")
            return ChatResponse(
                content="ok",
                model="flaky-model",
                prompt_tokens=3,
                completion_tokens=4,
                finish_reason="stop",
            )

        async def stream(self, request: ChatRequest):
            yield ChatResponse(
                content="ok",
                model="flaky-model",
                prompt_tokens=3,
                completion_tokens=4,
                finish_reason="stop",
            )

    provider = FlakyProvider()
    client = make_llm_client(provider)

    # Task 3 design: failure paths attribute to ``<unknown>`` because the
    # resolver could have raised before we picked a provider. Success path
    # uses the resolved provider name.
    before_unavail = _counter_value(
        LLM_CALLS_TOTAL,
        provider="<unknown>",
        model="flaky-model",
        route_mode="auto",
        outcome="unavailable",
    )
    before_success = _counter_value(
        LLM_CALLS_TOTAL,
        provider="flaky",
        model="flaky-model",
        route_mode="auto",
        outcome="success",
    )

    resp = await client.chat(
        ChatRequest(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            model="flaky-model",
        )
    )
    assert resp.content == "ok"
    assert provider.attempts == 2

    # One unavailable (first attempt failed), one success (second worked).
    assert _counter_value(
        LLM_CALLS_TOTAL,
        provider="<unknown>",
        model="flaky-model",
        route_mode="auto",
        outcome="unavailable",
    ) == before_unavail + 1
    assert _counter_value(
        LLM_CALLS_TOTAL,
        provider="flaky",
        model="flaky-model",
        route_mode="auto",
        outcome="success",
    ) == before_success + 1