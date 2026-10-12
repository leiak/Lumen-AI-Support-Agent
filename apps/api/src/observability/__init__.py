"""OpenTelemetry tracing setup for the Lumen API + workers.

The SDK is auto-configured via the standard ``OTEL_*`` env vars (see
``apps/api/.env.example``). Two things this module does explicitly:

1. Wire ``opentelemetry-instrumentation-httpx`` so outbound HTTP to
   LLM providers (anthropic / openai / minimax) is traced for free.
2. Provide a single ``tracer`` for the rest of the codebase to import.

Sampling, exporter endpoint, and resource attributes are all
controlled via env — no code changes needed to point at a different
backend. To disable tracing entirely: ``OTEL_SDK_DISABLED=true``.
"""
from __future__ import annotations

from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

# Production can flood the trace with high-volume, low-signal spans
# (Qdrant reads, Minio uploads, Postgres health probes) unless we
# filter the internal docker-compose hosts. ``OTEL_INSTRUMENTATION_
# HTTPX_EXCLUDED_URLS`` is a comma-separated list of URL substrings to
# skip. Default: the dev stack's internal services.
import os

_EXCLUDED_URLS = os.environ.get(
    "OTEL_INSTRUMENTATION_HTTPX_EXCLUDED_URLS",
    "qdrant,minio,postgres,localhost",
).split(",")

# HTTPXClientInstrumentor is idempotent — safe to call multiple times.
# Patches the global httpx.Client / httpx.AsyncClient so every outbound
# HTTP request (including those made by anthropic-sdk and openai-sdk,
# which both use httpx under the hood) emits a span. The span is a
# child of whatever current span is active, so it nests under
# ``llm.chat`` and similar. ``excluded_urls`` filters out internal
# services (Qdrant, Minio, Postgres, localhost health probes) so the
# trace is not flooded with low-signal spans.
HTTPXClientInstrumentor().instrument(excluded_urls=_EXCLUDED_URLS)

# Single tracer for the whole codebase. ``get_tracer("lumen")`` is
# cheap; calling it on every import is fine.
tracer = trace.get_tracer("lumen")

# M6.2 — re-export the @traced decorator + PII constants
from observability.tracing import (  # noqa: E402
    PII_FORBIDDEN,
    PII_WHITELIST,
    traced,
)

__all__ = ["tracer", "traced", "PII_WHITELIST", "PII_FORBIDDEN"]
