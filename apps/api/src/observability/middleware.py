"""OpenTelemetry-aware request middleware.

Replaces M1's ``request_context_middleware`` (which used a flat
``request_id``) with a richer version that:

1. Reads the W3C ``traceparent`` header to recover the upstream
   trace_id (if any).
2. Falls back to starting a new trace if no header is present.
3. Opens an ``http.request`` span around the entire request.
4. Binds ``trace_id`` (and the old ``request_id`` alias) into the
   structlog contextvars so log lines carry the same id.
5. Echoes ``X-Trace-Id`` on the response so the SPA / curl users can
   paste it into Tempo for cross-system correlation.
6. Also echoes ``X-Request-ID`` for back-compat with pre-M6 clients
   that read the old M1 header — both headers hold the same value.

Middleware ordering note
-------------------------
Starlette / FastAPI middlewares run in **reverse** order of
registration (LIFO): the last one added runs first on the request
and last on the response. To wrap everything (including CORS) without
swallowing exceptions, :func:`apps.api.main` adds this middleware
*after* :class:`CORSMiddleware` and the metrics decorator — see
``main.py``.

The :class:`starlette.middleware.base.BaseHTTPMiddleware` body wraps
``call_next`` in a ``try/except`` that re-raises after the span is
ended. FastAPI's exception handlers then convert the exception to a
proper HTTP response, but the span status is already set to ERROR
with the exception recorded as a span event.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import Request
from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.trace import Status, StatusCode
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from core.request_context import bind_trace_context, clear_request_context

# Routes we never trace (noise — health probes, prom scrape). Health
# endpoints are hit every few seconds by orchestrators and would
# dominate the trace count without adding any signal. /metrics is
# excluded for the same reason.
EXCLUDED_ROUTES = frozenset({"/health", "/health/live", "/health/ready", "/metrics"})


class TraceContextMiddleware(BaseHTTPMiddleware):
    """Inbound HTTP span + trace_id binding + ``X-Trace-Id`` echo."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Health / metrics endpoints: no span, no trace_id binding, no
        # header echo. Returning ``call_next`` directly is the cheapest
        # possible path — no OTel allocation, no structlog binding.
        if request.url.path in EXCLUDED_ROUTES:
            return await call_next(request)

        # Extract the upstream trace context from the W3C traceparent
        # header (if any). The OTel SDK auto-injects this for outbound
        # calls; for inbound we have to extract explicitly so we can
        # attach the upstream context and capture the trace_id for the
        # response header + structlog binding.
        carrier = dict(request.headers)
        parent_ctx = propagate.extract(carrier)
        token = otel_context.attach(parent_ctx) if parent_ctx is not None else None

        tracer = trace.get_tracer("lumen")
        try:
            with tracer.start_as_current_span(
                "http.request",
                attributes={
                    "http.method": request.method,
                    "http.route": request.url.path,
                },
            ) as span:
                trace_id = format(span.get_span_context().trace_id, "032x")
                bind_trace_context(trace_id=trace_id)
                t0 = time.perf_counter()
                try:
                    response = await call_next(request)
                except Exception as exc:
                    # Record the exception on the span and re-raise so
                    # FastAPI's exception handlers can convert it to
                    # an HTTP response. The response-header echo is
                    # unreachable in this path (no Response object),
                    # but the trace_id is still discoverable via the
                    # X-Trace-Id attached by FastAPI's exception
                    # handler middleware in production deployments.
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR))
                    raise
                finally:
                    # Clear the structlog contextvars even on success
                    # so the next request (in the same process) doesn't
                    # inherit our trace_id. ``finally`` runs after the
                    # ``except`` block above re-raises, so the
                    # contextvars are cleaned up before the exception
                    # propagates to FastAPI's handlers.
                    clear_request_context()
                # Happy path: enrich the span + echo headers.
                span.set_attribute("http.status_code", response.status_code)
                span.set_attribute(
                    "http.latency_ms", (time.perf_counter() - t0) * 1000
                )
                response.headers["X-Trace-Id"] = trace_id
                # M1 back-compat: also echo X-Request-ID. Value =
                # trace_id (single source of truth — request_id is
                # gone, trace_id is the new id). Pre-M6 clients that
                # read X-Request-ID keep working without changes; new
                # clients can read either header, they have the same
                # value.
                response.headers["X-Request-ID"] = trace_id
                return response
        finally:
            # Always detach the extracted parent context so the next
            # request doesn't accidentally inherit the previous
            # request's parent span. ``otel_context.attach`` returns a
            # token; ``detach`` with the token restores the prior
            # context. Passing None to detach is a no-op.
            if token is not None:
                otel_context.detach(token)


__all__ = ["EXCLUDED_ROUTES", "TraceContextMiddleware"]