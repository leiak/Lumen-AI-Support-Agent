"""Regression tests for :func:`agent.graph._safe.log_and_return`.

Background
----------

Task 10 introduced :func:`agent.graph._safe.safe_respond` to centralize
the ``except Exception: log + return fallback`` pattern that used to
be copy-pasted in three places (``make_retrieve_node``,
``make_llm_node`` initial call, and the tool-loop re-invoke).

But the three call sites were migrated with a **no-op lambda**::

    except Exception:
        return safe_respond(
            fn=lambda: {"final_text": FALLBACK_MESSAGE, ...},  # no-op
            fallback={"final_text": FALLBACK_MESSAGE, ...},
            event="agent.graph.llm_failed_unexpected",
            ...
        )

Because the lambda never raises, ``safe_respond``'s internal
``try: fn()`` always succeeded, no WARNING was ever emitted, and the
defense-in-depth observability that pre-Task-10 lived directly in
the ``except Exception`` block was silently dropped. Operators
grepping for ``agent.graph.llm_failed_unexpected`` /
``agent.graph.retrieve_failed_unexpected`` saw zero records even
when the safety net was firing on every customer turn.

These tests pin the contract that the new
:func:`agent.graph._safe.log_and_return` helper (and the migrated
call sites in :mod:`agent.graph.nodes` /
:mod:`agent.graph._tool_dispatch`) MUST emit a WARNING with
``error_type`` + ``error_message`` + the event name on every
unexpected-exception path.

Testability note
----------------

Both helpers use ``core.logging.get_logger`` (structlog), so the
WARNING joins the structured log chain in production. Tests use
``structlog.testing.capture_logs`` (which bypasses the configured
``logger_factory``) to assert on the raw event dict.
"""
from __future__ import annotations

from typing import Any

import pytest


def test_log_and_return_emits_warning_with_error_metadata() -> None:
    """``log_and_return`` MUST emit a WARNING with the event name and
    ``error_type`` / ``error_message`` metadata, and return the
    fallback unchanged.
    """
    from structlog.testing import capture_logs

    from agent.graph._safe import log_and_return

    fallback: dict[str, Any] = {"foo": "bar"}
    exc = RuntimeError("boom")

    with capture_logs() as cap_logs:
        result = log_and_return(
            fallback=fallback,
            event="agent.graph.test_unexpected",
            exc=exc,
            tenant_id="t1",
            conversation_id="c1",
        )

    assert result == fallback
    matching = [e for e in cap_logs if e.get("event") == "agent.graph.test_unexpected"]
    assert matching, (
        "WARNING with event=agent.graph.test_unexpected must be emitted, "
        f"got: {[e.get('event') for e in cap_logs]}"
    )
    rec = matching[0]
    assert rec["log_level"] == "warning"
    assert rec["error_type"] == "RuntimeError"
    assert rec["error_message"] == "boom"


def test_log_and_return_passes_through_extra_kwargs() -> None:
    """Breadcrumb kwargs (tenant_id, conversation_id, ...) MUST be
    attached to the WARNING event so an operator can grep for the
    tenant that hit the unexpected path.
    """
    from structlog.testing import capture_logs

    from agent.graph._safe import log_and_return

    with capture_logs() as cap_logs:
        log_and_return(
            fallback={"x": 1},
            event="agent.graph.test_kwargs",
            exc=ValueError("bad input"),
            tenant_id="tenant-42",
            conversation_id="conv-99",
        )

    matching = [e for e in cap_logs if e.get("event") == "agent.graph.test_kwargs"]
    assert matching
    rec = matching[0]
    assert rec["tenant_id"] == "tenant-42"
    assert rec["conversation_id"] == "conv-99"


def test_log_and_return_does_not_emit_warning_on_no_op() -> None:
    """Sanity: calling ``log_and_return`` does NOT short-circuit the
    warning — the caller has the exception in hand; the helper just
    logs and returns.
    """
    from structlog.testing import capture_logs

    from agent.graph._safe import log_and_return

    with capture_logs() as cap_logs:
        result = log_and_return(
            fallback={"ok": True},
            event="agent.graph.no_exception_path",
            exc=KeyError("not really raised"),
        )

    assert result == {"ok": True}
    matching = [e for e in cap_logs if e.get("event") == "agent.graph.no_exception_path"]
    assert matching  # the helper always logs (no fn wrapping, no try/except)
    assert matching[0]["error_type"] == "KeyError"


@pytest.mark.asyncio
async def test_llm_node_logs_on_unexpected_exception() -> None:
    """The migrated LLM-node call site MUST emit a WARNING when a
    truly unexpected exception bubbles up. Pins the Task-10 fix
    end-to-end.
    """
    from unittest.mock import AsyncMock, MagicMock

    from structlog.testing import capture_logs

    from agent.graph.nodes import FALLBACK_MESSAGE, make_llm_node
    from agent.graph.state import AgentState

    def _state() -> AgentState:
        return AgentState(
            tenant_id="t1",
            conversation_id="c1",
            messages=[],
            rag_messages=[],
            final_text=None,
        )

    async def boom_factory(tenant_id: str):
        client = MagicMock()
        client.chat = AsyncMock(side_effect=RuntimeError("unexpected library bug"))
        return client

    node = make_llm_node(
        llm_client_factory=boom_factory,
        model="test-model",
        tools=[],
    )

    with capture_logs() as cap_logs:
        result = await node(_state())

    assert result["final_text"] == FALLBACK_MESSAGE
    matching = [
        e
        for e in cap_logs
        if e.get("event") == "agent.graph.llm_failed_unexpected"
    ]
    assert matching, (
        "WARNING with event=agent.graph.llm_failed_unexpected must be emitted, "
        f"got: {[e.get('event') for e in cap_logs]}"
    )


@pytest.mark.asyncio
async def test_retrieve_node_logs_on_unexpected_exception() -> None:
    """The migrated retrieve-node call site MUST emit a WARNING when
    a truly unexpected exception bubbles up. Pins the Task-10 fix
    end-to-end.
    """
    from unittest.mock import AsyncMock, MagicMock

    from langchain_core.messages import HumanMessage
    from structlog.testing import capture_logs

    from agent.graph.nodes import make_retrieve_node
    from agent.graph.state import AgentState

    def _state() -> AgentState:
        return AgentState(
            tenant_id="t1",
            conversation_id="c1",
            messages=[HumanMessage(content="hello")],
            rag_messages=[],
            final_text=None,
        )

    rag_service = MagicMock()
    rag_service.build_context_for_query = AsyncMock(
        side_effect=RuntimeError("unexpected library bug")
    )

    node = make_retrieve_node(rag_service=rag_service)

    with capture_logs() as cap_logs:
        result = await node(_state())

    # RAG fallback returns empty rag_messages; the customer turn
    # continues with no retrieval context.
    assert result == {"rag_messages": []}
    matching = [
        e
        for e in cap_logs
        if e.get("event") == "agent.graph.retrieve_failed_unexpected"
    ]
    assert matching, (
        "WARNING with event=agent.graph.retrieve_failed_unexpected must be emitted, "
        f"got: {[e.get('event') for e in cap_logs]}"
    )
