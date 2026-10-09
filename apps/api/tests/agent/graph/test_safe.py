"""Unit tests for :func:`agent.graph._safe.safe_respond`.

Task 10 — centralize the ``except Exception: log + return fallback``
pattern that was copy-pasted three times in
``make_retrieve_node`` / ``make_llm_node`` (initial call) / the
tool-loop re-invoke. The helper MUST:

* log a WARNING (captured by ``structlog.testing.capture_logs``)
  carrying ``error_type`` + ``error_message`` + the caller's extra
  breadcrumb kwargs;
* NEVER log the exception repr / traceback (PII safety — the
  customer's text is in nearby call frames);
* return the caller's ``fallback`` dict when the wrapped fn raises;
* return the wrapped fn's result unchanged on success.

The helper uses ``core.logging.get_logger`` (structlog) so the
WARNING joins the rest of the codebase's structured log chain in
production. Tests use ``structlog.testing.capture_logs`` to bypass
the configured ``logger_factory`` and assert on the raw event dict.
"""
from __future__ import annotations

from typing import Any


def test_safe_respond_returns_fallback_and_logs() -> None:
    """When the wrapped fn raises, safe_respond returns the fallback
    and emits a WARNING with the event + error metadata.
    """
    from structlog.testing import capture_logs

    from agent.graph._safe import safe_respond

    def boom() -> dict[str, Any]:
        raise RuntimeError("kaboom")

    with capture_logs() as cap_logs:
        result = safe_respond(
            fn=boom,
            fallback={"final_text": "fallback", "escalated": False},
            event="agent.test.boom",
            tenant_id="t1",
            conversation_id="c1",
        )

    assert result == {"final_text": "fallback", "escalated": False}
    matching = [e for e in cap_logs if e.get("event") == "agent.test.boom"]
    assert matching, (
        f"WARNING with event=agent.test.boom must be emitted, "
        f"got: {[e.get('event') for e in cap_logs]}"
    )
    rec = matching[0]
    assert rec["log_level"] == "warning"
    assert rec["error_type"] == "RuntimeError"
    assert rec["error_message"] == "kaboom"
    assert rec["tenant_id"] == "t1"
    assert rec["conversation_id"] == "c1"


def test_safe_respond_returns_fn_result_on_success() -> None:
    """When the wrapped fn returns successfully, safe_respond returns
    its result AND does not emit a WARNING.
    """

    def ok() -> dict[str, Any]:
        return {"final_text": "ok"}

    from structlog.testing import capture_logs

    from agent.graph._safe import safe_respond

    with capture_logs() as cap_logs:
        result = safe_respond(
            fn=ok,
            fallback={"final_text": "fallback"},
            event="ignored",
        )

    assert result == {"final_text": "ok"}
    # No WARNING should be emitted on the happy path.
    assert not [e for e in cap_logs if e.get("log_level") == "warning"]
