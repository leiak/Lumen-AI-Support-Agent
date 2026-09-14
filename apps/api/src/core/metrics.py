"""Prometheus metrics for the AI Customer Service API.

Exposes a ``/metrics`` endpoint in Prometheus text exposition format plus a
lightweight ASGI middleware that records per-request counter and latency.
Dynamic path segments (integer ids, ULIDs, UUIDs) are collapsed to ``:id`` so
high-cardinality request paths don't explode the label space of the time
series.

The ``prometheus-client`` dependency is already declared in pyproject.toml but
was previously unwired; this module is Stage 10's "metrics" slice.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)
from starlette.requests import Request
from starlette.responses import Response

# ULIDs are 26-char Crockford base32 (digits + A-Z excluding I, L, O, U).
_PATH_PARAM_RE = re.compile(r"/\d{1,20}\b|[0-9A-HJKMNP-TV-Z]{26}")

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests handled",
    ("method", "path", "status"),
)
REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ("method", "path"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)


def _normalize_path(path: str) -> str:
    """Collapse numeric / ULID path segments to a stable ``:id`` label."""
    return _PATH_PARAM_RE.sub(":id", path)


async def prometheus_metrics_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Record request count + latency, re-raising so FastAPI handles errors."""
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        latency = time.perf_counter() - start
        path = _normalize_path(request.url.path)
        REQUEST_COUNT.labels(request.method, path, "500").inc()
        REQUEST_DURATION.labels(request.method, path).observe(latency)
        raise
    latency = time.perf_counter() - start
    path = _normalize_path(request.url.path)
    REQUEST_COUNT.labels(
        request.method, path, str(response.status_code)
    ).inc()
    REQUEST_DURATION.labels(request.method, path).observe(latency)
    return response


def render_metrics() -> Response:
    """Build a ``/metrics`` response in Prometheus text exposition format."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
