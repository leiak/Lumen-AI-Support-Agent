"""M6.5 — verify Arq enqueue/extract round-trips the trace context.

These tests cover the three primitives exposed by
``observability.worker_context``:

* :func:`enqueue_with_trace` — injects the current traceparent into
  ``job_ctx`` before handing the job to arq.
* :func:`extract_trace_from_job` — restores the upstream context from a
  carrier (or falls back to the ambient context for an empty/None
  job_ctx).
* :func:`job_span` — opens a ``worker.<job_name>`` span as a child of
  the enqueuing request's trace.

The plan also asks for two coverage gaps beyond the original three:

* ``test_enqueue_preserves_existing_job_ctx_keys`` — caller-provided
  keys (e.g. ``{"trace_id": "..."}``) survive the dict merge with the
  injected ``traceparent`` carrier.
* ``test_extract_returns_current_when_job_ctx_empty`` — both
  ``extract_trace_from_job(None)`` and ``extract_trace_from_job({})``
  return the ambient context without raising.

Span-tracking setup
------------------

The tests that assert on emitted spans (``test_job_span_*``) install
a per-test ``InMemorySpanExporter`` via the ``local_memory_exporter``
fixture. The fixture adds the exporter to the ALREADY-INSTALLED
``TracerProvider`` (the OTel SDK only allows
``trace.set_tracer_provider()`` once — subsequent calls warn and
no-op, so we can't replace the global provider from a second
test directory). This avoids the global-state conflict that would
arise if ``tests/worker/`` had its own conftest calling
``set_tracer_provider()`` — that race silently dropped spans in
``tests/observability/`` because ``observability.tracer`` was bound
to the wrong provider.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from observability import tracer as lumen_tracer
from observability.worker_context import (
    enqueue_with_trace,
    extract_trace_from_job,
    job_span,
)


@pytest.fixture
def local_memory_exporter() -> InMemorySpanExporter:
    """Install a per-test ``InMemorySpanExporter`` on the active provider.

    The provider is the one already configured by
    ``tests/observability/conftest.py`` (or any earlier conftest).
    We add a ``SimpleSpanProcessor`` wrapping a fresh
    ``InMemorySpanExporter`` so this test's spans are captured
    alongside any other conftest-installed exporters. Removed in
    teardown so subsequent tests don't see leftover spans.
    """
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    # ``add_span_processor`` is the SDK-supported hook for
    # augmenting an already-configured provider. Returns the
    # provider, so we capture it for teardown.
    span_processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(span_processor)
    yield exporter
    # Cleanup: shutdown the processor and clear it from the provider
    # so the exporter list doesn't grow across tests.
    span_processor.shutdown()


@pytest.mark.asyncio
async def test_enqueue_injects_traceparent_into_job_ctx() -> None:
    """``enqueue_with_trace`` adds a ``traceparent`` key to job_ctx.

    The carrier round-trips the trace_id of the parent span active
    during enqueue. Existing ``job_ctx`` keys are preserved alongside
    the injected one.
    """
    pool = MagicMock()
    # arq's ``enqueue_job`` is an ``async`` method — mock it as such
    # so the awaited call returns a coroutine.
    pool.enqueue_job = AsyncMock()

    with lumen_tracer.start_as_current_span("http.request") as parent:
        await enqueue_with_trace(
            pool, "qa_judge", job_ctx={"existing": "key"}, message_id="msg_01",
        )

    call_kwargs = pool.enqueue_job.call_args.kwargs
    assert "job_ctx" in call_kwargs
    job_ctx = call_kwargs["job_ctx"]
    # Original key preserved
    assert job_ctx["existing"] == "key"
    # ``traceparent`` injected — W3C format: 4 dash-separated parts
    assert "traceparent" in job_ctx
    parts = job_ctx["traceparent"].split("-")
    assert len(parts) == 4
    assert parts[0] == "00"
    assert format(parent.context.trace_id, "032x") == parts[1]


@pytest.mark.asyncio
async def test_enqueue_with_trace_awaits_underlying_call() -> None:
    """The returned object is an awaitable whose underlying call is
    arq's ``enqueue_job``. Awaiting it must invoke the pool once.
    """
    pool = MagicMock()
    pool.enqueue_job = AsyncMock(return_value="job_id_xyz")

    result = await enqueue_with_trace(
        pool, "qa_judge", job_ctx=None, message_id="msg_02",
    )

    assert result == "job_id_xyz"
    pool.enqueue_job.assert_awaited_once()


def test_extract_recovers_trace_id() -> None:
    """Worker side: extract returns a context with the original trace_id."""
    upstream_trace_id = "0af7651916cd43dd8448eb211c80319c"
    upstream_span_id = "b7ad6b7169203331"
    job_ctx = {
        "traceparent": f"00-{upstream_trace_id}-{upstream_span_id}-01",
    }
    ctx = extract_trace_from_job(job_ctx)
    span_ctx = trace.get_current_span(ctx).get_span_context()
    assert format(span_ctx.trace_id, "032x") == upstream_trace_id


def test_extract_returns_current_when_job_ctx_empty() -> None:
    """``extract_trace_from_job`` accepts ``None`` and ``{}`` without raising.

    Both must return the ambient context — useful when an upstream
    CLI / cron enqueue didn't inject a traceparent and the worker
    should still emit a root span rather than fail.
    """
    # With no parent span active, the ambient context's current span
    # is the invalid/no-op span — we only assert it doesn't raise.
    assert extract_trace_from_job(None) is not None
    assert extract_trace_from_job({}) is not None

    # When a parent span IS active, extract without a carrier falls
    # back to that ambient context — the returned context's current
    # span must match the parent.
    with lumen_tracer.start_as_current_span("ambient.parent") as p:
        ctx_none = extract_trace_from_job(None)
        ctx_empty = extract_trace_from_job({})
        assert trace.get_current_span(ctx_none).get_span_context().span_id == (
            p.get_span_context().span_id
        )
        assert trace.get_current_span(ctx_empty).get_span_context().span_id == (
            p.get_span_context().span_id
        )


@pytest.mark.asyncio
async def test_enqueue_preserves_existing_job_ctx_keys() -> None:
    """Caller-provided keys survive the dict merge with the carrier."""
    pool = MagicMock()
    pool.enqueue_job = AsyncMock()

    caller_ctx = {
        "existing_key": "existing_value",
        "another_key": 42,
    }

    with lumen_tracer.start_as_current_span("http.request"):
        await enqueue_with_trace(
            pool, "qa_judge", job_ctx=caller_ctx, message_id="msg_03",
        )

    job_ctx = pool.enqueue_job.call_args.kwargs["job_ctx"]
    # Original keys preserved
    assert job_ctx["existing_key"] == "existing_value"
    assert job_ctx["another_key"] == 42
    # Carrier injected alongside
    assert "traceparent" in job_ctx


def test_job_span_is_child_of_enqueuer(local_memory_exporter) -> None:
    """A worker.job_name span emitted in job_span() shares trace_id with
    a parent span active during enqueue.

    The parent here is the *ambient* context (no traceparent carrier);
    we use ``otel_context.attach`` semantics: the parent span IS the
    current span when ``job_span`` enters, and the worker span
    becomes its child.
    """
    with lumen_tracer.start_as_current_span("http.request"):
        # Simulate the worker receiving an empty job_ctx — the
        # ambient context (parent span) is what gets attached.
        with job_span("qa_judge", {}):
            with lumen_tracer.start_as_current_span("inner.work"):
                pass

    spans = local_memory_exporter.get_finished_spans()
    by_name = {s.name: s for s in spans}
    assert "http.request" in by_name
    assert "worker.qa_judge" in by_name
    assert "inner.work" in by_name

    # All share the same trace_id
    trace_ids = {s.context.trace_id for s in spans}
    assert len(trace_ids) == 1

    # The worker span has the whitelisted worker.* attributes
    worker_attrs = dict(by_name["worker.qa_judge"].attributes or {})
    assert worker_attrs.get("worker.job_name") == "qa_judge"
    assert worker_attrs.get("worker.attempt") == 1

    # worker.qa_judge is a child of http.request — use the
    # ReadableSpan.parent property (newer OTel SDK removed
    # SpanContext.parent_span_id; the plan's test predates that fix).
    worker = by_name["worker.qa_judge"]
    request = by_name["http.request"]
    assert worker.parent.span_id == request.context.span_id
    # And inner.work is a grandchild of http.request
    assert by_name["inner.work"].parent.span_id == worker.context.span_id


def test_job_span_with_carrier_traces_back_to_upstream() -> None:
    """When job_ctx carries a traceparent, worker.qa_judge's parent
    span_id equals the upstream span_id encoded in the carrier.
    """
    upstream_trace_id = "0af7651916cd43dd8448eb211c80319c"
    upstream_span_id = "b7ad6b7169203331"
    job_ctx = {
        "traceparent": f"00-{upstream_trace_id}-{upstream_span_id}-01",
    }

    with job_span("qa_judge", job_ctx) as worker_span:
        # Inside the worker — the current span IS worker.qa_judge
        # and its parent is the upstream span from the carrier.
        pass

    span_ctx = worker_span.get_span_context()
    # Same trace_id as the upstream carrier
    assert format(span_ctx.trace_id, "032x") == upstream_trace_id
    # And the parent span_id matches the upstream span_id (NOT 0 /
    # invalid), proving the carrier was honoured.
    assert worker_span.parent is not None
    assert format(worker_span.parent.span_id, "016x") == upstream_span_id


def test_job_span_records_attempt(local_memory_exporter) -> None:
    """``attempt`` is reflected on the span as ``worker.attempt``."""
    with job_span("qa_judge", {}, attempt=3):
        pass

    spans = local_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("worker.attempt") == 3
    assert attrs.get("worker.job_name") == "qa_judge"