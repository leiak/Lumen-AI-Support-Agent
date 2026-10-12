"""Per-request context propagation for structured logging and metrics.

Stage 11.2 adds a thin ContextVar layer that the request middleware uses
to bind ``request_id`` (and optionally tenant/conversation ids) into the
structlog ``contextvars`` chain. Because :func:`core.logging.configure_logging`
already installs :func:`structlog.contextvars.merge_contextvars` as the first
processor, every log call inside the request automatically carries the
bound fields without any explicit plumbing at the call site.

Why a separate module
---------------------
The :class:`fastapi.Request` object itself is the natural place to stash
per-request state, but it isn't accessible from background workers
(``arq`` tasks, the Qdrant indexer, etc.) which still want to record the
same ``request_id`` for cross-process log correlation. A ``ContextVar``
is the only mechanism that survives ``asyncio.create_task`` boundaries
without manual copy-on-spawn.

What this is NOT
----------------
This is **not** OpenTelemetry. We deliberately don't track spans, parent
ids, or causal relationships between calls — that's the M3 LLM Gateway
job (see ``docs/superpowers/plans/2026-09-10-ai-customer-m1.md`` §10.4).
For M1 close-out, the only thing we need is "every log line within a
single HTTP request shares the same ``request_id`` so we can grep them
together" — a flat correlation primitive, not a trace.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

import structlog

from core.id_gen import new_id

# Single ContextVar carrying the opaque request id. ``default=None`` so
# background tasks (no HTTP request in flight) get ``None`` rather than
# crashing. ``ContextVar`` is asyncio-safe and survives
# ``asyncio.create_task`` without explicit copy.
_current_request_id: ContextVar[str | None] = ContextVar(
    "current_request_id", default=None
)


def get_request_id() -> str | None:
    """Return the current request id, or ``None`` outside a request scope."""
    return _current_request_id.get()


def bind_request_context(
    *,
    request_id: str | None = None,
    tenant_id: str | None = None,
    conversation_id: str | None = None,
) -> str:
    """Bind request-scope fields into structlog's contextvars.

    Returns the *effective* request id (the one passed in, or a freshly
    generated ULID if ``request_id is None``) so the caller can echo it
    back to the client via the ``X-Request-ID`` response header without
    re-deriving the value.

    The ``tenant_id`` and ``conversation_id`` parameters are best-effort:
    if not supplied (which is the common case from the middleware, which
    runs *before* JWT decoding), the corresponding structlog field is
    simply absent. Callers downstream that already know the tenant (e.g.
    ``ConversationService.record_message``) can re-bind via
    ``structlog.contextvars.bind_contextvars(tenant_id=...)``.
    """
    rid = request_id or new_id()
    _current_request_id.set(rid)

    bind_kwargs: dict[str, Any] = {"request_id": rid}
    if tenant_id is not None:
        bind_kwargs["tenant_id"] = tenant_id
    if conversation_id is not None:
        bind_kwargs["conversation_id"] = conversation_id
    structlog.contextvars.bind_contextvars(**bind_kwargs)
    return rid


def clear_request_context() -> None:
    """Unbind every contextvar field. Always call this in a ``finally``.

    Safe to call when nothing is bound — structlog is idempotent here.
    """
    structlog.contextvars.clear_contextvars()
    _current_request_id.set(None)


def bind_trace_context(*, trace_id: str, span_id: str | None = None) -> str:
    """Bind ``trace_id`` (and optionally ``span_id``) into structlog contextvars.

    Returns the effective trace_id (always equal to the input). The
    existing ``request_id`` field is also set to ``trace_id`` so log
    lines keep their familiar structure — M6.6 promotes ``trace_id``
    to the primary correlation id; ``request_id`` is kept as a back-compat
    alias for downstream tooling that still greps logs by ``request_id``.

    Used by :class:`observability.middleware.TraceContextMiddleware` to
    make every log line emitted during a request carry the same OTel
    trace_id as the request's span.
    """
    payload: dict[str, Any] = {"trace_id": trace_id, "request_id": trace_id}
    if span_id is not None:
        payload["span_id"] = span_id
    structlog.contextvars.bind_contextvars(**payload)
    _current_request_id.set(trace_id)
    return trace_id


__all__ = [
    "bind_request_context",
    "bind_trace_context",
    "clear_request_context",
    "get_request_id",
]
