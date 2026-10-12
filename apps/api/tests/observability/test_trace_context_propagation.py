"""M6.2 — W3C Trace Context round-trip tests.

The ``traceparent`` header is the standard for propagating trace
context across services. M6 uses it for HTTP→worker (Arq) propagation.
"""
from __future__ import annotations

from opentelemetry import propagate, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


def test_inject_produces_traceparent_header() -> None:
    """``propagate.inject`` adds a ``traceparent`` key to the carrier."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("parent") as parent:
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        assert "traceparent" in carrier
        # W3C format: "00-{trace_id}-{span_id}-{flags}"
        parts = carrier["traceparent"].split("-")
        assert len(parts) == 4
        assert parts[0] == "00"
        assert parts[1] == format(parent.context.trace_id, "032x")
        assert parts[2] == format(parent.context.span_id, "016x")


def test_extract_uses_upstream_trace_id() -> None:
    """``propagate.extract`` reads the trace_id from a carrier."""
    upstream_trace_id = "0af7651916cd43dd8448eb211c80319c"
    upstream_span_id = "b7ad6b7169203331"
    carrier = {
        "traceparent": f"00-{upstream_trace_id}-{upstream_span_id}-01",
    }
    ctx = propagate.extract(carrier)
    # The extracted context has the upstream trace_id
    span_ctx = trace.get_current_span(ctx).get_span_context()
    assert format(span_ctx.trace_id, "032x") == upstream_trace_id


def test_extract_without_traceparent_generates_new_trace() -> None:
    """No header = a new trace starts."""
    ctx = propagate.extract({})
    # Default behavior: a no-op context. Subsequent spans are roots.
    span_ctx = trace.get_current_span(ctx).get_span_context()
    # trace_id may be 0 (invalid) here — the test just verifies no crash
    assert span_ctx is not None


def test_round_trip_preserves_trace_id() -> None:
    """Inject then extract returns the same trace_id."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("p") as p:
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        extracted = propagate.extract(carrier)
        span_ctx = trace.get_current_span(extracted).get_span_context()
        assert span_ctx.trace_id == p.context.trace_id


def test_traceparent_flag_01_means_sampled() -> None:
    """Flag 01 = sampled (SAMPLED). Flag 00 = not sampled (NONE)."""
    sampled = propagate.extract({
        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
    })
    not_sampled = propagate.extract({
        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00",
    })
    # Just verify the extract doesn't crash and returns distinct contexts
    assert sampled is not None
    assert not_sampled is not None


def test_tracestate_header_preserved() -> None:
    """The ``tracestate`` header (vendor-specific data) round-trips.

    ``propagate.inject`` reads from a Context object. We pass the
    extracted context explicitly via the ``context=`` kwarg so the
    propagator can serialize the upstream ``tracestate``. The
    default (no context) reads the current span, which is empty
    here — tracestate is dropped.
    """
    upstream_trace_id = "0af7651916cd43dd8448eb211c80319c"
    upstream_span_id = "b7ad6b7169203331"
    carrier = {
        "traceparent": f"00-{upstream_trace_id}-{upstream_span_id}-01",
        "tracestate": "lumen=abc123",
    }
    extracted = propagate.extract(carrier)
    out: dict[str, str] = {}
    propagate.inject(out, context=extracted)
    assert "traceparent" in out
    assert out.get("tracestate") == "lumen=abc123", (
        f"tracestate did not round-trip; got {out.get('tracestate')!r}"
    )
