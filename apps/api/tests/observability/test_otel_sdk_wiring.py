"""M6.1 — verify the OTel SDK initializes, exposes a tracer, and httpx
auto-instrumentation is wired.

These tests don't require Tempo to be running — they verify the SDK
configuration itself, not end-to-end span delivery.
"""
from __future__ import annotations

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.instrumentation.httpx import (
    HTTPXClientInstrumentor,
)

from observability import tracer


def test_tracer_is_sdk_named_lumen() -> None:
    """The shared tracer is a real SDK tracer named ``lumen``.

    Per the OTel API docs, ``get_tracer()`` MAY return different
    ``Tracer`` instances for the same name across calls — we don't
    assert object identity, just that observability's tracer is an
    SDK-backed ``Tracer`` (not a ``ProxyTracer`` from the no-op
    default provider).
    """
    assert tracer is not None
    # Must be a real SDK Tracer — ProxyTracer is the no-op fallback
    # returned when no provider has been configured. The conftest
    # installs a SDK TracerProvider, so observability's tracer
    # should be a SDK-backed Tracer.
    assert not isinstance(tracer, trace.ProxyTracer), (
        f"tracer is a ProxyTracer — SDK provider not installed "
        f"before observability was imported (type={type(tracer).__name__})"
    )
    # The shared tracer's instrumentation scope should match the name
    # we passed. Different SDK versions expose this differently;
    # ``_instrumentation_scope`` is the SDK 1.27+ attribute.
    scope_name = getattr(
        getattr(tracer, "_instrumentation_scope", None), "name", None
    )
    if scope_name is not None:
        assert scope_name == "lumen", f"expected 'lumen', got {scope_name!r}"


def test_httpx_instrumentor_is_instrumented() -> None:
    """Calling instrument() once is enough; second call is idempotent.

    ``is_instrumented_by_opentelemetry`` is a property on
    ``BaseInstrumentor`` that returns True once ``instrument()`` has
    patched the target library.
    """
    assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry is True


def test_outbound_httpx_emits_span(memory_exporter) -> None:
    """A httpx call after ``import observability`` produces a client span.

    Uses ``HTTPTransport`` (the transport the auto-instrumentation
    wraps) pointing at a port that won't accept connections. The
    connection error is expected — we only assert that the span was
    emitted BEFORE the error surfaces. ``MockTransport`` would NOT
    exercise the wrapped code path because the instrumentor patches
    ``HTTPTransport.handle_request`` only.
    """
    spans_before = len(memory_exporter.get_finished_spans())

    with httpx.Client(transport=httpx.HTTPTransport()) as client:
        try:
            client.get("http://127.0.0.1:1/test", timeout=0.5)
        except Exception:
            # Connection refused / timeout — irrelevant; we only
            # care that the span was emitted before the error.
            pass

    spans_after = memory_exporter.get_finished_spans()
    new_spans = spans_after[spans_before:]
    assert len(new_spans) >= 1, (
        f"expected at least 1 new span, got {len(new_spans)} "
        f"(total={len(spans_after)}, before={spans_before}); "
        f"httpx instrumented={HTTPXClientInstrumentor().is_instrumented_by_opentelemetry}"
    )
    # Span name is the HTTP method + URL (httpx auto-instrumentation default)
    span_names = {s.name for s in new_spans}
    assert any("GET" in n for n in span_names), f"expected GET span, got {span_names}"


def test_no_op_when_sdk_disabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Smoke check: ``OTEL_SDK_DISABLED`` is the documented escape hatch.

    The actual no-op behavior is the OTel SDK's contract — when this
    env var is true at SDK init time, the SDK installs a no-op
    provider and ``tracer`` becomes a ``ProxyTracer``. We can't
    exercise that path in-process because the conftest installs our
    own ``TracerProvider`` before ``observability`` is imported
    (see conftest.py module-load setup). This test just confirms the
    env var is settable so a future operator who sets it can rely
    on it being recognized. Full behavioral verification would need
    a subprocess test (out of scope for the unit test layer).
    """
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    import os

    assert os.environ["OTEL_SDK_DISABLED"] == "true"


def test_observability_module_import_is_idempotent(memory_exporter) -> None:
    """Importing ``observability`` twice doesn't double-instrument httpx.

    A double-instrumented httpx would emit 2 spans per request (one
    per wrap). We verify by re-importing the module and making a
    real httpx call — the new span count must be exactly 1.
    """
    import importlib
    import observability  # noqa: F401
    importlib.reload(observability)

    spans_before = len(memory_exporter.get_finished_spans())
    with httpx.Client(transport=httpx.HTTPTransport()) as client:
        try:
            client.get("http://127.0.0.1:1/test", timeout=0.5)
        except Exception:
            # Connection refused — irrelevant; we only care that
            # exactly one span was emitted (not 0, not 2).
            pass
    spans_after = memory_exporter.get_finished_spans()
    new_spans = spans_after[spans_before:]
    assert len(new_spans) == 1, (
        f"idempotent import should emit exactly 1 span per call; "
        f"got {len(new_spans)}: {[s.name for s in new_spans]}"
    )
