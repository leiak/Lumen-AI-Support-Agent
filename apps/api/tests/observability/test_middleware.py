"""M6.6 — verify ``TraceContextMiddleware`` wires the trace_id through.

Tests exercise the inbound side of W3C Trace Context propagation:

* No upstream ``traceparent`` → the middleware opens a fresh trace
  and echoes the generated ``X-Trace-Id``.
* Upstream ``traceparent`` present → the response ``X-Trace-Id``
  matches the upstream trace_id (so a browser / curl user can follow
  the trace across services).
* Health endpoints (``/health``, ``/metrics``, etc.) are excluded
  from tracing — no span, no header.
* 5xx responses still echo ``X-Trace-Id`` (so the client can report
  it to support).
* The middleware opens an ``http.request`` span with the agreed
  attributes (``http.method``, ``http.route``, ``http.status_code``).
* Two parallel requests get distinct trace_ids — ContextVar isolation
  is preserved by ``BaseHTTPMiddleware``'s per-request context copy.
* ``X-Request-ID`` is echoed alongside ``X-Trace-Id`` with the same
  value (M1 back-compat for pre-M6 clients).

The ``memory_exporter`` and ``clear_memory_exporter`` fixtures come
from ``tests/observability/conftest.py``, which installs an in-memory
``TracerProvider`` at module-load time so ``observability.tracer`` is
bound to it.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from observability.middleware import TraceContextMiddleware


def _make_app() -> FastAPI:
    """Build a minimal FastAPI app with ``TraceContextMiddleware``.

    Routes:
        ``GET /echo``  — happy path; returns ``{"ok": True}``.
        ``GET /health`` — excluded route; returns ``{"status": "live"}``.
        ``GET /fail``  — raises ``HTTPException(500)`` so we can test
                         that error responses still echo ``X-Trace-Id``.
    """
    app = FastAPI()

    @app.get("/echo")
    async def echo() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/fail")
    async def fail() -> dict[str, str]:
        raise HTTPException(status_code=500, detail="boom")

    app.add_middleware(TraceContextMiddleware)
    return app


def test_no_traceparent_generates_trace_id_and_echoes_it() -> None:
    """No upstream ``traceparent`` → server generates a new trace_id."""
    client = TestClient(_make_app())
    response = client.get("/echo")
    assert response.status_code == 200
    assert "X-Trace-Id" in response.headers
    trace_id = response.headers["X-Trace-Id"]
    # 32-char lowercase hex (W3C trace_id format)
    assert len(trace_id) == 32
    int(trace_id, 16)  # parses as hex — raises if not


def test_traceparent_header_uses_upstream_trace_id() -> None:
    """Upstream ``traceparent`` → response ``X-Trace-Id`` matches."""
    client = TestClient(_make_app())
    upstream_trace_id = "0af7651916cd43dd8448eb211c80319c"
    upstream_span_id = "b7ad6b7169203331"
    response = client.get(
        "/echo",
        headers={
            "traceparent": f"00-{upstream_trace_id}-{upstream_span_id}-01",
        },
    )
    assert response.status_code == 200
    assert response.headers["X-Trace-Id"] == upstream_trace_id


def test_health_endpoint_does_not_echo_trace_id() -> None:
    """Excluded routes skip the trace_id binding and header echo."""
    client = TestClient(_make_app())
    response = client.get("/health")
    assert response.status_code == 200
    # Health endpoints are excluded — no X-Trace-Id header
    assert "X-Trace-Id" not in response.headers
    assert "X-Request-ID" not in response.headers


def test_request_with_error_response_still_echoes_trace_id() -> None:
    """A 500 response still echoes ``X-Trace-Id`` so support can grep."""
    # ``raise_server_exceptions=False`` so the test client surfaces
    # the HTTPException instead of letting it propagate to the test
    # runner. The middleware still attaches the header to the
    # response object FastAPI's handler builds.
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/fail")
    assert response.status_code == 500
    assert "X-Trace-Id" in response.headers
    trace_id = response.headers["X-Trace-Id"]
    assert len(trace_id) == 32
    int(trace_id, 16)


def test_middleware_emits_http_request_span(memory_exporter) -> None:
    """The middleware opens an ``http.request`` span with the agreed attrs."""
    client = TestClient(_make_app())
    client.get("/echo")

    spans = memory_exporter.get_finished_spans()
    http_spans = [s for s in spans if s.name == "http.request"]
    assert len(http_spans) == 1
    attrs = dict(http_spans[0].attributes or {})
    assert attrs.get("http.method") == "GET"
    assert attrs.get("http.route") == "/echo"
    assert attrs.get("http.status_code") == 200
    # latency is recorded as a float attribute on the span
    assert "http.latency_ms" in attrs
    assert isinstance(attrs["http.latency_ms"], (int, float))


def test_concurrent_requests_get_distinct_trace_ids() -> None:
    """Two sequential requests get distinct trace_ids (no cross-leakage).

    Note: ``TestClient`` is synchronous and processes requests one at
    a time within a single event loop — true concurrency would need
    ``httpx.AsyncClient`` against the ASGI app. We verify that the
    ContextVar-reset in ``finally`` prevents the second request from
    inheriting the first's trace_id.
    """
    client = TestClient(_make_app())
    r1 = client.get("/echo")
    r2 = client.get("/echo")
    assert "X-Trace-Id" in r1.headers
    assert "X-Trace-Id" in r2.headers
    assert r1.headers["X-Trace-Id"] != r2.headers["X-Trace-Id"]


def test_middleware_echoes_x_request_id_for_back_compat() -> None:
    """``X-Request-ID`` is also returned with the same value as ``X-Trace-Id``."""
    client = TestClient(_make_app())
    response = client.get("/echo")
    assert "X-Request-ID" in response.headers
    assert "X-Trace-Id" in response.headers
    assert response.headers["X-Request-ID"] == response.headers["X-Trace-Id"]