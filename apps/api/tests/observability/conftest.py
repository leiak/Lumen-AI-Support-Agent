"""Shared test fixtures for OpenTelemetry observability tests.

The SDK tracer provider MUST be installed at module-load time (i.e.
before any test module does ``from observability import tracer``),
otherwise ``observability/__init__.py`` will cache a ``ProxyTracer``
from the no-op default provider and the auto-instrumentation will
write spans to nothing.

The ``memory_exporter`` fixture exposes the exporter used by the
session-scoped provider; ``clear_memory_exporter`` resets the span
list between tests so each one starts with an empty buffer.
"""
from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

# Module-level SDK setup: this runs once during conftest collection,
# BEFORE the test module is imported, so ``observability.tracer`` is
# bound to the in-memory exporter from the start.
_exporter = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_exporter))
trace.set_tracer_provider(_provider)


@pytest.fixture(scope="session")
def memory_exporter() -> InMemorySpanExporter:
    """Expose the session-wide in-memory span exporter.

    Spans are accessed via ``memory_exporter.get_finished_spans()``.
    Cleared at the start of each test via the
    ``clear_memory_exporter`` fixture.
    """
    return _exporter


@pytest.fixture(autouse=True)
def clear_memory_exporter(memory_exporter: InMemorySpanExporter) -> None:
    """Reset the in-memory span list before each test."""
    memory_exporter.clear()
