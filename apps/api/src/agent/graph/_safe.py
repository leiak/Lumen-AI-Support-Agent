"""Defensive wrappers for agent graph nodes.

Two helpers with distinct use-cases:

* :func:`safe_respond` — for the ``try/except`` pattern where you wrap a
  *function call* that may raise. The helper invokes ``fn`` and on
  exception logs a WARNING then returns ``fallback``. Use when the
  call site reads like ``try: fn() except Exception: ...``.

* :func:`log_and_return` — for the bare ``except Exception as exc:``
  pattern where you already have the exception in hand. The helper
  unconditionally logs a WARNING then returns ``fallback``. Use when
  the call site reads like ``except Exception as exc: ...``.

Both helpers route their log line through ``core.logging.get_logger``
(structlog's :class:`BoundLogger`) so the WARNING joins the rest of
the codebase's structured log chain — operators get a JSON event with
``event``, ``error_type``, ``error_message`` and the breadcrumb
kwargs (``tenant_id``, ``conversation_id``, ...) instead of a flat
``WARNING:agent.graph._safe:...`` stdlib line.

Why two helpers instead of one?
-------------------------------

Task 10 introduced only :func:`safe_respond` (the ``fn``-wrapping
case). The three production call sites that were migrated to it
passed a **no-op lambda**::

    except Exception:
        return safe_respond(
            fn=lambda: {<fallback dict>},  # never raises
            fallback={<fallback dict>},
            event="agent.graph.llm_failed_unexpected",
            ...
        )

The ``fn`` never raised, so the internal ``try: fn()`` always
succeeded, no WARNING was ever emitted, and the defense-in-depth
observability that pre-Task-10 lived directly in the
``except Exception`` block was silently dropped. Operators grepping
for ``agent.graph.llm_failed_unexpected`` /
``agent.graph.retrieve_failed_unexpected`` saw zero records even
when the safety net was firing on every customer turn.

:func:`log_and_return` exists for exactly that ``except
Exception as exc:`` shape — it does NOT wrap a callable, it just
logs the exception you already have and returns the fallback. The
three production call sites have been migrated to use it.

Testability
-----------

``structlog.testing.capture_logs`` is the canonical context manager
for asserting on structlog output in unit tests (it bypasses
``logger_factory`` entirely). Both helpers' tests use it.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.logging import get_logger

_log = get_logger(__name__)


def safe_respond(
    *,
    fn: Callable[[], Any],
    fallback: Any,
    event: str,
    **log_extra: Any,
) -> Any:
    """Invoke ``fn``; on any exception, log WARNING and return ``fallback``.

    The exception class name is logged as ``error_type``; the
    exception message as ``error_message``; the exception's repr /
    traceback is NEVER logged (PII safety — the customer's text is in
    nearby call frames).

    Parameters
    ----------
    fn:
        Zero-arg callable that performs the work. May raise any
        exception type.
    fallback:
        Value returned to the caller when ``fn`` raises. Shape must
        match the LangGraph partial-state contract the caller
        already uses (``final_text`` / ``escalated`` /
        ``tool_iterations`` / ``rag_messages``).
    event:
        structlog-style event name (e.g.
        ``"agent.graph.retrieve_failed_unexpected"``) — emitted as
        the WARNING log message so an operator can grep for it.
    **log_extra:
        Opaque breadcrumb kwargs forwarded as event fields to the
        WARNING record. Typical keys: ``tenant_id``,
        ``conversation_id``.
    """
    try:
        return fn()
    except Exception as exc:
        # Typed exceptions may already have been logged at a more
        # specific level; this catches truly unexpected library bugs.
        _log.warning(
            event,
            error_type=type(exc).__name__,
            error_message=str(exc),
            **log_extra,
        )
        return fallback


def log_and_return(
    *,
    fallback: Any,
    event: str,
    exc: BaseException,
    **log_extra: Any,
) -> Any:
    """Log an unexpected error at WARNING then return the fallback.

    Distinct from :func:`safe_respond` (which wraps a ``fn``): this
    helper is for the bare ``except Exception as exc:`` branch where
    you already have the exception in hand and just want to log it +
    return the fallback dict.

    The exception class name is logged as ``error_type``; the
    exception message as ``error_message``; the exception's repr /
    traceback is NEVER logged (PII safety).

    Uses structlog's :func:`core.logging.get_logger` so the log line
    joins the rest of the codebase's structured log chain.

    Parameters
    ----------
    fallback:
        Value returned to the caller. Shape must match the LangGraph
        partial-state contract the caller already uses
        (``final_text`` / ``escalated`` / ``tool_iterations`` /
        ``rag_messages``).
    event:
        structlog-style event name (e.g.
        ``"agent.graph.llm_failed_unexpected"``) — emitted as the
        WARNING log message so an operator can grep for it.
    exc:
        The exception caught in the ``except`` block. Used for
        ``error_type`` and ``error_message`` only — never repr'd.
    **log_extra:
        Opaque breadcrumb kwargs forwarded as event fields to the
        WARNING record. Typical keys: ``tenant_id``,
        ``conversation_id``.
    """
    _log.warning(
        event,
        error_type=type(exc).__name__,
        error_message=str(exc),
        **log_extra,
    )
    return fallback
