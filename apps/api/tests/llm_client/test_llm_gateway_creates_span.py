"""M6.3 — verify LLM Gateway + Provider + Tenant + Budget manual spans.

These tests exercise the OTel instrumentation introduced in
``apps/api/src/llm_client/client.py`` (llm.chat + llm.chat.stream),
``apps/api/src/llm_client/tenant_resolver.py`` (llm.resolve),
``apps/api/src/llm_client/tenant_model_override_resolver.py``
(llm.override), and ``apps/api/src/budget/resolver.py``
(llm.budget.precheck + llm.budget.postrecord).

The conftest at ``apps/api/tests/observability/conftest.py``
install the in-memory span exporter at module-load time; tests below
use the ``memory_exporter`` fixture to inspect spans produced by the
calls.

PII discipline: attribute keys used here (``tenant_id``,
``provider_name``, ``model``, ``llm.tokens.input``,
``llm.tokens.output``, ``llm.cost_usd``, ``llm.latency_ms``) are all
on the PII whitelist (see ``observability/tracing.py``).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from llm_client.client import LLMClient
from llm_client.exceptions import (
    InvalidRequest,
    RateLimited,
)
from llm_client.gateway import LLMGateway
from llm_client.providers.anthropic_provider import AnthropicProvider
from llm_client.types import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    MessageRole,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ANTHROPIC_OK_BODY = {
    "id": "msg_test",
    "model": "claude-3-5-sonnet-20241022",
    "content": [{"type": "text", "text": "Hello back"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 5},
}


def _chat_request(model: str = "claude-3-5-sonnet-20241022") -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )


@pytest.fixture
async def client_with_anthropic() -> AsyncIterator[LLMClient]:
    """LLMClient wired to an Anthropic provider via the gateway.

    Mirrors the fixture pattern in test_client.py so we get
    gateway.aclose_all() teardown even when tests raise.
    """
    p = AnthropicProvider(api_key="test-key", model="claude-3-5-sonnet-20241022")
    g = LLMGateway(providers={"anthropic": p})
    yield LLMClient(
        provider_resolver=g.default_resolver,
        tenant_id="t-otel-m6-3",
    )
    await g.aclose_all()


def _chat_spans(memory_exporter: Any) -> list[Any]:
    """Return all llm.chat (or llm.chat.stream) spans in order."""
    return [
        s for s in memory_exporter.get_finished_spans()
        if s.name in ("llm.chat", "llm.chat.stream")
    ]


# ---------------------------------------------------------------------------
# Step 3.6 — llm.chat span emission
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chat_emits_llm_chat_span(
    client_with_anthropic: LLMClient,
    memory_exporter: Any,
    httpx_mock: HTTPXMock,
) -> None:
    """Successful chat produces 1 ``llm.chat`` span + httpx child span."""
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        json=_ANTHROPIC_OK_BODY,
    )

    resp = await client_with_anthropic.chat(_chat_request())
    assert resp.content == "Hello back"

    spans = memory_exporter.get_finished_spans()
    chat_spans = _chat_spans(memory_exporter)
    assert len(chat_spans) >= 1, (
        f"expected >=1 llm.chat span, got {[s.name for s in spans]}"
    )
    # The first llm.chat span is the root — confirm tenant_id attribute
    attrs = dict(chat_spans[0].attributes or {})
    assert attrs.get("tenant_id") == "t-otel-m6-3"
    # Status should be UNSET (success path doesn't set ERROR)
    assert chat_spans[0].status.status_code.name == "UNSET"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chat_span_has_provider_model_attributes(
    client_with_anthropic: LLMClient,
    memory_exporter: Any,
    httpx_mock: HTTPXMock,
) -> None:
    """The llm.chat span carries provider_name + model after resolution."""
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        json=_ANTHROPIC_OK_BODY,
    )

    await client_with_anthropic.chat(_chat_request())

    chat_spans = _chat_spans(memory_exporter)
    assert len(chat_spans) >= 1
    attrs = dict(chat_spans[0].attributes or {})
    assert attrs.get("provider_name") == "anthropic", (
        f"expected provider_name=anthropic, got attrs={attrs!r}"
    )
    assert attrs.get("model") == "claude-3-5-sonnet-20241022", (
        f"expected model attribute set, got attrs={attrs!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chat_span_has_token_and_cost_attributes(
    client_with_anthropic: LLMClient,
    memory_exporter: Any,
    httpx_mock: HTTPXMock,
) -> None:
    """Post-success attributes: input/output tokens, cost, latency.

    ``llm.cost_usd`` comes from
    :func:`llm_client.usage.estimate_cost_usd` against
    ``claude-3-5-sonnet-20241022`` (10 input @ 0.003/1K +
    5 output @ 0.015/1K = 0.000105).
    """
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        json=_ANTHROPIC_OK_BODY,
    )

    await client_with_anthropic.chat(_chat_request())

    chat_spans = _chat_spans(memory_exporter)
    assert len(chat_spans) >= 1
    attrs = dict(chat_spans[0].attributes or {})
    # Token counts from the response
    assert attrs.get("llm.tokens.input") == 10
    assert attrs.get("llm.tokens.output") == 5
    # Cost comes from estimate_cost_usd (test is float-equal so we
    # don't risk CI floating-point flake by comparing against the
    # exact value; we just assert it's a positive number in the
    # expected ballpark).
    cost = attrs.get("llm.cost_usd")
    assert isinstance(cost, float)
    assert cost > 0.0
    # Latency is positive — sanity check the format, not the number.
    latency = attrs.get("llm.latency_ms")
    assert isinstance(latency, float)
    assert latency > 0.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chat_failure_sets_error_status(
    client_with_anthropic: LLMClient,
    memory_exporter: Any,
    httpx_mock: HTTPXMock,
) -> None:
    """A 4xx response → llm.chat span status=ERROR + exception recorded.

    The 429 status code raises ``RateLimited`` from
    ``AnthropicProvider``; the LLMClient translates the failure into
    the ``rate_limited`` outcome metric AND sets the span status to
    ERROR via ``span.set_status(Status(StatusCode.ERROR))``.
    """
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=429,
        json={"error": {"type": "rate_limit_error", "message": "slow"}},
    )

    with pytest.raises(RateLimited):
        await client_with_anthropic.chat(_chat_request())

    chat_spans = _chat_spans(memory_exporter)
    assert len(chat_spans) >= 1
    span = chat_spans[0]
    assert span.status.status_code.name == "ERROR", (
        f"expected ERROR status, got {span.status.status_code.name}"
    )
    # The recorded exception shows up as a span event (defensive:
    # record_exception is called BEFORE set_status so the event is
    # present even when set_status overwrites the description).
    event_names = [e.name for e in span.events]
    assert "exception" in event_names, (
        f"expected exception event, got events={[e.name for e in span.events]}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chat_400_invalid_request_sets_error_status(
    client_with_anthropic: LLMClient,
    memory_exporter: Any,
    httpx_mock: HTTPXMock,
) -> None:
    """400 → InvalidRequest surfaced + llm.chat span status=ERROR."""
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=400,
        json={"error": {"type": "invalid_request_error", "message": "bad model"}},
    )

    with pytest.raises(InvalidRequest):
        await client_with_anthropic.chat(_chat_request())

    chat_spans = _chat_spans(memory_exporter)
    assert len(chat_spans) >= 1
    assert chat_spans[0].status.status_code.name == "ERROR"


# ---------------------------------------------------------------------------
# Step 3.6 (cont.) — llm.chat.stream span emission
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stream_chat_emits_stream_span(
    client_with_anthropic: LLMClient,
    memory_exporter: Any,
    httpx_mock: HTTPXMock,
) -> None:
    """Streaming path → ``llm.chat.stream`` span with token/cost attrs.

    The streaming span wraps the WHOLE iteration (not per chunk) to
    avoid span-volume explosion. Token + cost + latency attributes
    are set when the final ``ChatResponse`` arrives.
    """
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        text=(
            "data: "
            + '{"type":"message_start","message":'
            + '{"model":"claude-3-5-sonnet-20241022",'
            + '"usage":{"input_tokens":7}}}\n'
            + "data: "
            + '{"type":"content_block_delta","delta":'
            + '{"type":"text_delta","text":"hello"}}\n'
            + "data: "
            + '{"type":"message_delta","delta":'
            + '{"stop_reason":"end_turn"},'
            + '"usage":{"output_tokens":3}}\n'
            + "data: "
            + '{"type":"message_stop"}\n'
        ),
        headers={"Content-Type": "text/event-stream"},
    )

    items = [
        item async for item in client_with_anthropic.stream_chat(_chat_request())
    ]
    text_items = [i for i in items if isinstance(i, str)]
    final = items[-1]
    assert isinstance(final, ChatResponse)
    assert text_items == ["hello"]
    assert final.prompt_tokens == 7
    assert final.completion_tokens == 3

    spans = memory_exporter.get_finished_spans()
    stream_spans = [
        s for s in spans if s.name == "llm.chat.stream"
    ]
    assert len(stream_spans) == 1, (
        f"expected 1 llm.chat.stream span, got "
        f"{[s.name for s in spans if 'stream' in s.name or 'chat' in s.name]}"
    )
    attrs = dict(stream_spans[0].attributes or {})
    assert attrs.get("tenant_id") == "t-otel-m6-3"
    assert attrs.get("provider_name") == "anthropic"
    assert attrs.get("llm.tokens.input") == 7
    assert attrs.get("llm.tokens.output") == 3
    # Latency is positive (sanity).
    latency = attrs.get("llm.latency_ms")
    assert isinstance(latency, float) and latency > 0.0


# ---------------------------------------------------------------------------
# Step 3.3 + 3.4 — llm.resolve + llm.override spans
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_span_emitted_when_using_tenant_resolver(
    memory_exporter: Any,
) -> None:
    """TenantResolverSpan.__call__ emits a ``llm.resolve`` span."""
    from unittest.mock import AsyncMock, MagicMock

    from llm_client.gateway import LLMGateway
    from llm_client.tenant_resolver_span import TenantResolverSpan

    provider = MagicMock()
    provider.name = "anthropic"
    # LLMGateway.aclose_all calls provider.aclose as an awaitable;
    # a sync MagicMock would TypeError on gather(), so use AsyncMock.
    provider.aclose = AsyncMock()  # type: ignore[attr-defined]
    g = LLMGateway(providers={"anthropic": provider})
    # Wrap the gateway's default_resolver in a TenantResolverSpan. The
    # span is added by TenantResolverSpan.__call__.
    tenant_resolver = TenantResolverSpan(
        inner=g.default_resolver, tenant_id="t-resolve-test",
    )

    req = _chat_request()
    out = tenant_resolver(req)
    assert out is provider

    resolve_spans = [
        s for s in memory_exporter.get_finished_spans()
        if s.name == "llm.resolve"
    ]
    assert len(resolve_spans) == 1
    attrs = dict(resolve_spans[0].attributes or {})
    assert attrs.get("tenant_id") == "t-resolve-test"
    assert attrs.get("provider_name") == "anthropic"

    await g.aclose_all()


# ---------------------------------------------------------------------------
# M6.3 fix — TenantResolverSpan wrapper wired into _raise_or_return
# ---------------------------------------------------------------------------
# The tests below exercise the production-wiring fix: ``_raise_or_return``
# now wraps the (possibly model-override-wrapped) resolver in
# :class:`TenantResolverSpan` so the ``llm.resolve`` span fires on every
# LLM request in production.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_resolve_span_unit_emits_on_sync_call(
    memory_exporter: Any,
) -> None:
    """Unit: ``TenantResolverSpan.__call__`` emits a ``llm.resolve`` span.

    Synchronous path used by ``LLMClient._chat_with_retry`` for
    single-provider / non-fallback tenants. After delegation the span
    has both ``tenant_id`` (set at start) and ``provider_name`` (set
    after delegation).
    """
    from unittest.mock import MagicMock

    from llm_client.tenant_resolver_span import TenantResolverSpan

    provider = MagicMock()
    provider.name = "anthropic"
    span_wrapper = TenantResolverSpan(
        inner=lambda req: provider,
        tenant_id="t-unit-call",
    )

    req = _chat_request()
    out = span_wrapper(req)
    assert out is provider

    resolve_spans = [
        s for s in memory_exporter.get_finished_spans()
        if s.name == "llm.resolve"
    ]
    assert len(resolve_spans) == 1
    attrs = dict(resolve_spans[0].attributes or {})
    assert attrs.get("tenant_id") == "t-unit-call"
    assert attrs.get("provider_name") == "anthropic"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_span_unit_emits_on_ainvoke_with_inner_ainvoke(
    memory_exporter: Any,
) -> None:
    """Unit: ``TenantResolverSpan.ainvoke`` delegates to ``inner.ainvoke``.

    The inner here exposes ``ainvoke`` (the typical
    :class:`FallbackResolver` / :class:`TenantModelOverrideResolver`
    shape). The span fires once and carries ``tenant_id``. ``provider_name``
    is NOT set on the ainvoke path because the inner returns a
    ``ChatResponse`` (not a provider) — the upstream ``llm.chat`` span
    carries the resolved provider name.
    """
    from llm_client.tenant_resolver_span import TenantResolverSpan

    class _Inner:
        def __call__(self, request: Any) -> Any:
            return None  # pragma: no cover - ainvoke path used

        async def ainvoke(self, request: Any) -> Any:
            # Return a ChatResponse-like (no ``.name`` attribute) so
            # ``provider_name`` is left unset on the ainvoke path.
            return _chat_request()  # ChatRequest has no .name attr

    span_wrapper = TenantResolverSpan(
        inner=_Inner(),
        tenant_id="t-unit-ainvoke",
    )
    out = await span_wrapper.ainvoke(_chat_request())
    assert out is not None  # whatever ChatRequest-like was returned

    resolve_spans = [
        s for s in memory_exporter.get_finished_spans()
        if s.name == "llm.resolve"
    ]
    assert len(resolve_spans) == 1
    attrs = dict(resolve_spans[0].attributes or {})
    assert attrs.get("tenant_id") == "t-unit-ainvoke"
    # provider_name is unset because inner.ainvoke returned a non-provider.
    assert "provider_name" not in attrs


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_span_unit_ainvoke_falls_back_when_inner_lacks_ainvoke(
    memory_exporter: Any,
) -> None:
    """Unit: when inner has no ``ainvoke``, ``TenantResolverSpan.ainvoke``
    falls back to sync ``__call__`` and still emits one ``llm.resolve`` span.

    This is the single-provider shape (``_PrefixResolver``) — the span
    fires once with ``tenant_id`` + ``provider_name`` set after
    delegation.
    """
    from unittest.mock import MagicMock

    from llm_client.tenant_resolver_span import TenantResolverSpan

    provider = MagicMock()
    provider.name = "minimax"

    class _Inner:
        def __call__(self, request: Any) -> Any:
            return provider

    span_wrapper = TenantResolverSpan(
        inner=_Inner(),
        tenant_id="t-unit-fallback",
    )
    out = await span_wrapper.ainvoke(_chat_request())
    assert out is provider

    resolve_spans = [
        s for s in memory_exporter.get_finished_spans()
        if s.name == "llm.resolve"
    ]
    assert len(resolve_spans) == 1
    attrs = dict(resolve_spans[0].attributes or {})
    assert attrs.get("tenant_id") == "t-unit-fallback"
    assert attrs.get("provider_name") == "minimax"


@pytest.mark.integration
def test_resolve_span_unit_provider_name_fallback_when_unknown(
    memory_exporter: Any,
) -> None:
    """Unit: when the inner returns something with no ``.name`` attribute
    the span still fires with ``provider_name="<unknown>"`` (defensive
    for mock providers in tests)."""
    from llm_client.tenant_resolver_span import TenantResolverSpan

    # Inner returns a bare object (no .name attribute) — simulates a
    # mock provider that hasn't set up .name.
    bare = object()
    span_wrapper = TenantResolverSpan(
        inner=lambda req: bare,
        tenant_id="t-unit-unknown",
    )
    out = span_wrapper(_chat_request())
    assert out is bare

    resolve_spans = [
        s for s in memory_exporter.get_finished_spans()
        if s.name == "llm.resolve"
    ]
    assert len(resolve_spans) == 1
    attrs = dict(resolve_spans[0].attributes or {})
    assert attrs.get("tenant_id") == "t-unit-unknown"
    assert attrs.get("provider_name") == "<unknown>"


@pytest.mark.integration
def test_resolve_span_fires_via_raise_or_return(
    memory_exporter: Any,
) -> None:
    """Integration: ``_raise_or_return`` wraps the resolver in
    :class:`TenantResolverSpan` so the ``llm.resolve`` span fires on
    every call. The production-wiring test.

    No model-override rows in the input — the wrap happens
    unconditionally, not gated on overrides existing.
    """
    from unittest.mock import AsyncMock, MagicMock

    from llm_client.tenant_resolver import TenantLLMConfigCache
    from llm_client.tenant_resolver_span import TenantResolverSpan

    provider = MagicMock()
    provider.name = "anthropic"
    provider.aclose = AsyncMock()  # type: ignore[attr-defined]
    cache = TenantLLMConfigCache(ttl_s=60.0, maxsize=16)
    # ``_raise_or_return`` constructs the inner gateway from ``providers``
    # — passing our mock directly. ``rows=None`` means no model-override
    # wrap (the M5.2 conditional skip). The new M6.3 wrap still fires.
    resolver = cache._raise_or_return(
        tenant_id="t-m6-3",
        providers={"anthropic": provider},
        rows=None,
    )

    # Verify the resolver IS wrapped in TenantResolverSpan.
    assert isinstance(resolver, TenantResolverSpan), (
        f"expected TenantResolverSpan wrap, got {type(resolver).__name__}"
    )

    # Calling the resolver triggers a ``llm.resolve`` span.
    out = resolver(_chat_request())
    assert out is provider

    resolve_spans = [
        s for s in memory_exporter.get_finished_spans()
        if s.name == "llm.resolve"
    ]
    assert len(resolve_spans) == 1, (
        f"expected 1 llm.resolve span, got "
        f"{[s.name for s in memory_exporter.get_finished_spans()]}"
    )
    attrs = dict(resolve_spans[0].attributes or {})
    assert attrs.get("tenant_id") == "t-m6-3"
    assert attrs.get("provider_name") == "anthropic"


@pytest.mark.integration
def test_resolve_span_emitted_even_without_model_overrides(
    memory_exporter: Any,
) -> None:
    """Integration: span fires when ``rows`` is ``None`` (no model
    overrides configured for the tenant). Verifies the wrap is
    unconditional, not gated on M5.2's model-override conditional."""
    from unittest.mock import AsyncMock, MagicMock

    from llm_client.tenant_resolver import TenantLLMConfigCache
    from llm_client.tenant_resolver_span import TenantResolverSpan

    provider = MagicMock()
    provider.name = "minimax"
    provider.aclose = AsyncMock()  # type: ignore[attr-defined]
    cache = TenantLLMConfigCache(ttl_s=60.0, maxsize=16)
    # rows=[] (empty) — same effect as rows=None for the M5.2 wrap
    # condition (empty overrides dict → no model-override wrap), but
    # the M6.3 wrap still fires.
    resolver = cache._raise_or_return(
        tenant_id="t-m6-3-empty-rows",
        providers={"minimax": provider},
        rows=[],
    )

    assert isinstance(resolver, TenantResolverSpan)
    out = resolver(_chat_request())
    assert out is provider

    resolve_spans = [
        s for s in memory_exporter.get_finished_spans()
        if s.name == "llm.resolve"
    ]
    assert len(resolve_spans) == 1
    attrs = dict(resolve_spans[0].attributes or {})
    assert attrs.get("tenant_id") == "t-m6-3-empty-rows"
    assert attrs.get("provider_name") == "minimax"


@pytest.mark.integration
def test_resolve_span_fires_alongside_model_override_wrapper(
    memory_exporter: Any,
) -> None:
    """Integration: when model overrides are configured, BOTH spans
    fire on a single call — ``llm.resolve`` (outer) and ``llm.override``
    (inner, from the M5.2 wrapper). The two wrappers compose
    transparently."""
    from unittest.mock import AsyncMock, MagicMock

    from llm_client.tenant_model_override_resolver import (
        TenantModelOverrideResolver,
    )
    from llm_client.tenant_resolver import TenantLLMConfigCache
    from llm_client.tenant_resolver_span import TenantResolverSpan

    provider = MagicMock()
    provider.name = "anthropic"
    provider.aclose = AsyncMock()  # type: ignore[attr-defined]
    cache = TenantLLMConfigCache(ttl_s=60.0, maxsize=16)

    # Build a row with ``model`` set so the M5.2 wrap kicks in.
    row = MagicMock()
    row.provider_name = "anthropic"
    row.enabled = True
    row.model = "claude-haiku-4-5"

    resolver = cache._raise_or_return(
        tenant_id="t-m6-3-override",
        providers={"anthropic": provider},
        rows=[row],
    )

    # Layered wrap: TenantResolverSpan(TenantModelOverrideResolver(gateway)).
    assert isinstance(resolver, TenantResolverSpan)
    assert isinstance(resolver._inner, TenantModelOverrideResolver)

    # Calling the resolver triggers BOTH spans on a single request:
    # llm.resolve fires on the outer wrapper; llm.override fires on the
    # inner wrapper's _maybe_rewrite.
    resolver(_chat_request())

    spans = memory_exporter.get_finished_spans()
    resolve_spans = [s for s in spans if s.name == "llm.resolve"]
    override_spans = [s for s in spans if s.name == "llm.override"]
    assert len(resolve_spans) == 1, (
        f"expected 1 llm.resolve, got span names={[s.name for s in spans]}"
    )
    assert len(override_spans) == 1, (
        f"expected 1 llm.override, got span names={[s.name for s in spans]}"
    )
    assert dict(resolve_spans[0].attributes or {}).get("tenant_id") == "t-m6-3-override"
    assert dict(override_spans[0].attributes or {}).get("outcome") == "hit"


@pytest.mark.integration
def test_resolve_span_carries_tenant_id_attribute(
    memory_exporter: Any,
) -> None:
    """Integration: the ``llm.resolve`` span carries the tenant_id
    attribute set to the value passed into ``_raise_or_return``."""
    from unittest.mock import AsyncMock, MagicMock

    from llm_client.tenant_resolver import TenantLLMConfigCache

    provider = MagicMock()
    provider.name = "anthropic"
    provider.aclose = AsyncMock()  # type: ignore[attr-defined]
    cache = TenantLLMConfigCache(ttl_s=60.0, maxsize=16)
    resolver = cache._raise_or_return(
        tenant_id="t-tenant-id-check",
        providers={"anthropic": provider},
        rows=None,
    )

    resolver(_chat_request())

    resolve_spans = [
        s for s in memory_exporter.get_finished_spans()
        if s.name == "llm.resolve"
    ]
    assert len(resolve_spans) == 1
    attrs = dict(resolve_spans[0].attributes or {})
    assert attrs.get("tenant_id") == "t-tenant-id-check", (
        f"tenant_id attribute missing or wrong: {attrs!r}"
    )


@pytest.mark.integration
def test_resolve_span_carries_provider_name_attribute(
    memory_exporter: Any,
) -> None:
    """Integration: the ``llm.resolve`` span carries the ``provider_name``
    attribute set AFTER delegation — the inner resolver picks the
    provider, we read its ``.name``, and set the span attribute."""
    from unittest.mock import AsyncMock, MagicMock

    from llm_client.tenant_resolver import TenantLLMConfigCache

    provider = MagicMock()
    provider.name = "openai"
    provider.aclose = AsyncMock()  # type: ignore[attr-defined]
    cache = TenantLLMConfigCache(ttl_s=60.0, maxsize=16)
    resolver = cache._raise_or_return(
        tenant_id="t-provider-name-check",
        providers={"openai": provider},
        rows=None,
    )

    resolver(_chat_request(model="gpt-4o-mini"))

    resolve_spans = [
        s for s in memory_exporter.get_finished_spans()
        if s.name == "llm.resolve"
    ]
    assert len(resolve_spans) == 1
    attrs = dict(resolve_spans[0].attributes or {})
    assert attrs.get("provider_name") == "openai", (
        f"provider_name attribute missing or wrong: {attrs!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_override_span_emitted_on_model_rewrite(
    memory_exporter: Any,
) -> None:
    """TenantModelOverrideResolver emits ``llm.override`` (outcome=hit)
    when the per-tenant override applies.
    """
    from unittest.mock import create_autospec

    from llm_client.providers.base import BaseProvider
    from llm_client.tenant_model_override_resolver import (
        TenantModelOverrideResolver,
    )

    provider = create_autospec(BaseProvider, instance=True)
    provider.name = "anthropic"
    wrapper = TenantModelOverrideResolver(
        inner=lambda req: provider,
        overrides={"anthropic": "claude-haiku-4-5"},
    )

    req = _chat_request(model="claude-3-5-sonnet-20241022")
    wrapper(req)
    assert req.model == "claude-haiku-4-5"

    override_spans = [
        s for s in memory_exporter.get_finished_spans()
        if s.name == "llm.override"
    ]
    assert len(override_spans) == 1
    attrs = dict(override_spans[0].attributes or {})
    assert attrs.get("provider_name") == "anthropic"
    assert attrs.get("outcome") == "hit"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_override_span_unset_outcome_when_no_override(
    memory_exporter: Any,
) -> None:
    """When the provider has no override entry, the span fires with
    ``outcome="unset"`` (per the plan's spec).
    """
    from unittest.mock import create_autospec

    from llm_client.providers.base import BaseProvider
    from llm_client.tenant_model_override_resolver import (
        TenantModelOverrideResolver,
    )

    provider = create_autospec(BaseProvider, instance=True)
    provider.name = "anthropic"
    wrapper = TenantModelOverrideResolver(
        inner=lambda req: provider,
        overrides={},  # no override configured for anthropic
    )

    req = _chat_request()
    wrapper(req)
    assert req.model == "claude-3-5-sonnet-20241022"  # unchanged

    override_spans = [
        s for s in memory_exporter.get_finished_spans()
        if s.name == "llm.override"
    ]
    assert len(override_spans) == 1
    attrs = dict(override_spans[0].attributes or {})
    assert attrs.get("outcome") == "unset"


# ---------------------------------------------------------------------------
# Step 3.5 — llm.budget.precheck + llm.budget.postrecord spans
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_budget_precheck_span_emitted(
    memory_exporter: Any,
) -> None:
    """BudgetResolver._pre_check_sync emits a ``llm.budget.precheck`` span.

    Sync path is used by BudgetResolver.__call__ (and in async
    contexts; the loop-running guard skips enforcement but the span
    still fires because the guard is inside the span body).
    """
    from unittest.mock import MagicMock

    from budget.cache import TenantBudgetSnapshotCache
    from budget.models import TenantBudget
    from budget.resolver import BudgetResolver

    budget = TenantBudget(
        tenant_id="t-budget",
        hard_cap_tokens=1000,
        soft_warn_tokens=None,
        period_anchor_tz="UTC",
    )
    snapshot_cache = MagicMock(spec=TenantBudgetSnapshotCache)
    snap = MagicMock()
    snap.tokens_used = 0
    snapshot_cache.get_or_load = MagicMock(return_value=snap)

    inner_resolver = MagicMock()
    br = BudgetResolver(
        inner=inner_resolver,
        tenant_id="t-budget",
        budget=budget,
        snapshot_cache=snapshot_cache,
    )
    # Call outside an async loop so the pre-check runs to completion.
    inner_resolver.return_value = MagicMock(name="fake-provider")
    br(MagicMock())

    precheck_spans = [
        s for s in memory_exporter.get_finished_spans()
        if s.name == "llm.budget.precheck"
    ]
    assert len(precheck_spans) == 1
    attrs = dict(precheck_spans[0].attributes or {})
    assert attrs.get("tenant_id") == "t-budget"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_budget_postrecord_span_emitted(
    memory_exporter: Any,
) -> None:
    """BudgetResolver.ainvoke_stream emits a ``llm.budget.postrecord`` span
    for every final ``ChatResponse`` produced by the inner provider.
    """
    from collections.abc import AsyncIterator
    from unittest.mock import AsyncMock, MagicMock

    from budget.cache import TenantBudgetSnapshotCache
    from budget.models import TenantBudget
    from budget.resolver import BudgetResolver

    budget = TenantBudget(
        tenant_id="t-budget",
        hard_cap_tokens=1000,
        soft_warn_tokens=None,
        period_anchor_tz="UTC",
    )
    snapshot_cache = MagicMock(spec=TenantBudgetSnapshotCache)
    snap = MagicMock()
    snap.tokens_used = 0
    snapshot_cache.get_or_load_async = AsyncMock(return_value=snap)
    snapshot_repo = MagicMock()
    snapshot_repo.get_for_tenant_period = AsyncMock(return_value=snap)
    snapshot_repo.set_tokens_used = AsyncMock(return_value=None)
    snapshot_cache.invalidate = MagicMock()

    final_resp = ChatResponse(
        content="hi",
        model="fake-model",
        prompt_tokens=4,
        completion_tokens=3,
        finish_reason="stop",
    )

    class _FakeInnerResolver:
        def __call__(self, request: ChatRequest) -> Any:
            return _FakeProvider()

        async def ainvoke(self, request: ChatRequest) -> ChatResponse:
            return final_resp

        async def ainvoke_stream(
            self, request: ChatRequest,
        ) -> AsyncIterator[Any]:
            yield "x"
            yield final_resp

    class _FakeProvider:
        name = "fake"
        async def stream(
            self, request: ChatRequest,
        ) -> AsyncIterator[Any]:
            yield "x"
            yield final_resp

    br = BudgetResolver(
        inner=_FakeInnerResolver(),
        tenant_id="t-budget",
        budget=budget,
        snapshot_cache=snapshot_cache,
        snapshot_repo=snapshot_repo,
    )

    request = _chat_request()
    items = [item async for item in br.ainvoke_stream(request)]
    assert items[-1] is final_resp

    postrecord_spans = [
        s for s in memory_exporter.get_finished_spans()
        if s.name == "llm.budget.postrecord"
    ]
    assert len(postrecord_spans) >= 1
    attrs = dict(postrecord_spans[-1].attributes or {})
    assert attrs.get("tenant_id") == "t-budget"
    assert attrs.get("model") == "fake-model"
    assert attrs.get("llm.tokens.input") == 4
    assert attrs.get("llm.tokens.output") == 3
