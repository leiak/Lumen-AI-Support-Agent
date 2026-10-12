"""Unit tests for LLMClient.stream_chat using a fake provider (no DB/network).

Streaming cannot run against a live provider in unit tests, so we drive the
async-generator protocol with a lightweight in-memory provider and verify both
the delta forwarding and the usage-accounting behaviour on the final
ChatResponse.
"""
from collections.abc import AsyncGenerator, AsyncIterator

import pytest

from llm_client.client import LLMClient
from llm_client.gateway import LLMGateway
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


@pytest.fixture
async def client_with_fake() -> AsyncGenerator[LLMClient, None]:
    """LLMClient wired to the fake streaming provider; gateway cleaned up.

    Replaces the previous ``_make_client()`` async generator, which only
    ran its ``aclose_all()`` teardown if the consumer fully awaited the
    generator (i.e. called ``aclose()`` after ``__anext__()``). Using a
    pytest fixture guarantees the teardown runs even when tests raise.
    Mirrors the pattern in ``test_client.py::client_with_anthropic``.
    """
    g = LLMGateway(providers={"fake": _FakeStreamProvider()})
    yield LLMClient(
        provider_resolver=g.default_resolver,
        tenant_id="t-1",
    )
    await g.aclose_all()


async def test_stream_chat_forwards_deltas_then_final_response(
    client_with_fake: LLMClient,
) -> None:
    items = [item async for item in client_with_fake.stream_chat(_make_request())]

    assert [i for i in items if isinstance(i, str)] == ["one", "two"]
    assert isinstance(items[-1], ChatResponse)
    assert items[-1].content == "onetwo"
    assert items[-1].finish_reason == "stop"


async def test_stream_chat_records_usage_on_final_response(
    client_with_fake: LLMClient,
) -> None:
    items = [item async for item in client_with_fake.stream_chat(_make_request())]
    # Collect to completion ensures the final sentinel was consumed.
    assert isinstance(items[-1], ChatResponse)

    assert len(client_with_fake.usage._pending) == 1
    row = client_with_fake.usage._pending[0]
    assert row["tenant_id"] == "t-1"
    assert row["provider"] == "fake"
    assert row["prompt_tokens"] == 3
    assert row["completion_tokens"] == 2


class _FakeBudgetedResolver:
    """Fake ``BudgetResolver``-shaped resolver that records pre/post-call hooks.

    Mirrors the M4.D ``BudgetResolver`` contract:

    * ``__call__(request)`` returns the inner provider object — same as
      the real resolver's M4.A protocol shim. ``stream_chat`` uses this
      for the ``provider.name`` metric label.
    * ``ainvoke_stream(request)`` is the new async streaming seam
      (Explore #3 fix). It runs ``pre_check_callable()`` once before
      yielding, then for every final ``ChatResponse`` it calls
      ``post_record_callable(tokens_consumed)``. Items are forwarded
      transparently from the inner provider.

    The recorded call-list lets the regression tests assert that BOTH
    pre-check and post-record fired (the bug pre-fix had neither fire
    on the streaming path).
    """

    def __init__(
        self,
        inner: BaseProvider,
        *,
        pre_check_callable,
        post_record_callable,
    ) -> None:
        self._inner = inner
        self._pre_check = pre_check_callable
        self._post_record = post_record_callable

    def __call__(self, request: ChatRequest) -> BaseProvider:
        return self._inner

    async def ainvoke_stream(
        self, request: ChatRequest
    ) -> AsyncIterator["ChatResponse | str"]:
        await self._pre_check()
        async for item in self._inner.stream(request):
            if isinstance(item, ChatResponse):
                tokens = (
                    getattr(item, "prompt_tokens", 0)
                    + getattr(item, "completion_tokens", 0)
                )
                if tokens > 0:
                    # Mirror production BudgetResolver.ainvoke_stream:
                    # post-record failure must NOT abort the customer's
                    # stream — log + swallow so the token consumption
                    # (already done at the provider) still surfaces as
                    # a completed turn.
                    try:
                        await self._post_record(tokens)
                    except Exception:
                        pass
            yield item


async def test_stream_chat_invokes_budget_pre_check_and_post_record() -> None:
    """Regression test for Explore #3.

    Before the fix, ``LLMClient.stream_chat`` resolved the provider via
    the sync ``__call__`` shim whose ``_pre_check_sync`` silently
    skipped (loop-running guard) AND whose ``__call__`` never ran
    post-record. Net effect: streaming tenants accumulated no
    ``tenant_budget_snapshots`` rows and the cap was never enforced.

    After the fix, ``stream_chat`` detects the budget-aware resolver's
    ``ainvoke_stream`` method and routes through it — pre-check runs
    once, post-record runs for every final ``ChatResponse``.
    """
    provider = _FakeStreamProvider()
    pre_check_calls: list[int] = []
    post_record_calls: list[int] = []

    async def _pre_check() -> None:
        pre_check_calls.append(1)

    async def _post_record(tokens: int) -> None:
        post_record_calls.append(tokens)

    resolver = _FakeBudgetedResolver(
        provider,
        pre_check_callable=_pre_check,
        post_record_callable=_post_record,
    )
    client = LLMClient(
        provider_resolver=resolver,  # type: ignore[arg-type]
        tenant_id="t-budget",
    )

    items = [item async for item in client.stream_chat(_make_request())]

    # Forwarding unchanged
    assert isinstance(items[-1], ChatResponse)
    assert items[-1].content == "onetwo"

    # Pre-check fired exactly once (NOT skipped as the loop-running
    # guard used to make it). Post-record fired exactly once, with the
    # right token total (3 prompt + 2 completion = 5).
    assert len(pre_check_calls) == 1, (
        f"expected pre_check to fire once, got {len(pre_check_calls)} — "
        "loop-running guard is still bypassing the budget pre-check"
    )
    assert post_record_calls == [5], (
        f"expected post_record=[5], got {post_record_calls} — "
        "stream_chat is still not writing to tenant_budget_snapshots"
    )

    # Usage accounting also ran (llm_usage side of the contract).
    assert len(client.usage._pending) == 1
    assert client.usage._pending[0]["prompt_tokens"] == 3
    assert client.usage._pending[0]["completion_tokens"] == 2


async def test_stream_chat_budget_post_record_failure_does_not_abort_stream() -> None:
    """A post-record exception must NOT take down the customer's stream.

    The token consumption already happened at the provider; failing to
    record it is a soft miss on the budget tracking, not a stream error.
    """
    provider = _FakeStreamProvider()

    async def _pre_check() -> None:
        return None

    async def _post_record(tokens: int) -> None:
        raise RuntimeError("simulated DB blip on tenant_budget_snapshots UPSERT")

    resolver = _FakeBudgetedResolver(
        provider,
        pre_check_callable=_pre_check,
        post_record_callable=_post_record,
    )
    client = LLMClient(
        provider_resolver=resolver,  # type: ignore[arg-type]
        tenant_id="t-budget",
    )

    # All items must flow through — post_record raising must be swallowed.
    items = [item async for item in client.stream_chat(_make_request())]
    assert isinstance(items[-1], ChatResponse)
    assert items[-1].content == "onetwo"
    # And the llm_usage side still ran.
    assert len(client.usage._pending) == 1
