"""Unit tests for :func:`agent.graph._safe.safe_respond`.

Task 10 — centralize the ``except Exception: log + return fallback``
pattern that was copy-pasted three times in
``make_retrieve_node`` / ``make_llm_node`` (initial call) / the
tool-loop re-invoke. The helper MUST:

* log a WARNING (captured by ``caplog``) carrying ``error_type`` and
  the caller's extra breadcrumb kwargs;
* NEVER log the exception repr / traceback (PII safety — the
  customer's text is in nearby call frames);
* return the caller's ``fallback`` dict when the wrapped fn raises;
* return the wrapped fn's result unchanged on success.

The helper uses stdlib ``logging`` rather than ``structlog`` so that
the ``caplog`` fixture (stdlib-only) can observe the WARNING. This
matches the Task 4 (CORS startup warning) precedent.
"""
from __future__ import annotations

import logging
from typing import Any

import pytest

from agent.graph._safe import safe_respond


def test_safe_respond_returns_fallback_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the wrapped fn raises, safe_respond returns the fallback and logs WARNING."""

    def boom() -> dict[str, Any]:
        raise RuntimeError("kaboom")

    with caplog.at_level(logging.WARNING):
        result = safe_respond(
            fn=boom,
            fallback={"final_text": "fallback", "escalated": False},
            event="agent.test.boom",
            tenant_id="t1",
            conversation_id="c1",
        )
    assert result == {"final_text": "fallback", "escalated": False}
    assert any("agent.test.boom" in rec.message for rec in caplog.records)


def test_safe_respond_returns_fn_result_on_success() -> None:
    """When the wrapped fn returns successfully, safe_respond returns its result."""

    def ok() -> dict[str, Any]:
        return {"final_text": "ok"}

    result = safe_respond(
        fn=ok,
        fallback={"final_text": "fallback"},
        event="ignored",
    )
    assert result == {"final_text": "ok"}
