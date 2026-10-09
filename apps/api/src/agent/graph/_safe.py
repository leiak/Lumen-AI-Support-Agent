"""Defensive wrappers for agent graph nodes.

``safe_respond`` is the standard "log + return fallback" pattern used
in every node where a non-fatal failure should not crash the customer's
turn. Centralizing it removes 3 copy-pasted except blocks in
``make_retrieve_node``, ``make_llm_node`` (initial call), and the tool
loop (post-dispatch re-invoke).

Why stdlib ``logging`` (not ``structlog``)?
-------------------------------------------

``core.logging`` configures ``structlog`` with a ``PrintLoggerFactory``
and a ``JSONRenderer`` — the WARNING output never crosses the stdlib
``logging`` module, so pytest's ``caplog`` fixture (stdlib-only) cannot
observe it. To keep the helper unit-testable we emit via
``logging.getLogger(__name__)`` so the WARNING is captured by both
``caplog`` in tests AND the configured structlog pipeline in
production (structlog's default logger factory respects stdlib log
records). This matches the Task 4 (CORS startup warning) precedent.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

_log = logging.getLogger(__name__)


def safe_respond(
    *,
    fn: Callable[[], dict[str, Any]],
    fallback: dict[str, Any],
    event: str,
    **log_extra: Any,
) -> dict[str, Any]:
    """Invoke ``fn``; on any exception, log WARNING and return ``fallback``.

    The exception class name is logged as ``error_type``; the
    exception itself is NEVER logged with repr or traceback (PII
    safety — the customer's text is in nearby call frames).

    Parameters
    ----------
    fn:
        Zero-arg callable that performs the work. May raise any
        exception type.
    fallback:
        Dict returned to the caller when ``fn`` raises. Shape must
        match the LangGraph partial-state contract the caller
        already uses (``final_text`` / ``escalated`` /
        ``tool_iterations`` / ``rag_messages``).
    event:
        structlog-style event name (e.g.
        ``"agent.graph.retrieve_failed_unexpected"``) — emitted as
        the WARNING log message so an operator can grep for it.
    **log_extra:
        Opaque breadcrumb kwargs forwarded as ``extra=...`` to the
        WARNING record. Typical keys: ``tenant_id``,
        ``conversation_id``, ``error_type``.
    """
    try:
        return fn()
    except Exception as exc:
        _log.warning(
            "%s error_type=%s",
            event,
            type(exc).__name__,
            extra=log_extra,
        )
        return fallback
