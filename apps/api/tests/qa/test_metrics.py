"""Tests for the Stage 14 / Task 7 ``lumen_qa_*`` Prometheus metrics.

Each test snapshots a counter's value before the operation, performs
the operation, then asserts the value changed by exactly the expected
amount with the expected labels. Matches the pattern from
``tests/core/test_business_metrics.py`` — we use the module-level
counters directly because they're process-global by design.
"""
from __future__ import annotations

from typing import Any

from core.business_metrics import (
    LUMEN_QA_FAILURES,
    LUMEN_QA_FLAGGED,
    LUMEN_QA_SCORE_LATENCY,
    LUMEN_QA_SCORES,
    LUMEN_SLA_BREACHED,
)


def _counter_value(metric: Any, **labels: str) -> float:
    """Read the current value of a labelled counter.

    Prometheus stores child counters by their label tuple; we read via
    ``_value`` to avoid depending on the public sample API.
    """
    if labels:
        child = metric.labels(**labels)
    else:
        # Unlabelled metric (LUMEN_QA_FLAGGED).
        child = metric
    return child._value.get()  # type: ignore[attr-defined]  # noqa: SLF001


def test_lumen_qa_scores_increments() -> None:
    before = _counter_value(
        LUMEN_QA_SCORES, dimension="overall", bucket="high"
    )
    LUMEN_QA_SCORES.labels(dimension="overall", bucket="high").inc()
    after = _counter_value(
        LUMEN_QA_SCORES, dimension="overall", bucket="high"
    )
    assert after == before + 1


def test_lumen_qa_flagged_increments() -> None:
    before = _counter_value(LUMEN_QA_FLAGGED)
    LUMEN_QA_FLAGGED.inc()
    after = _counter_value(LUMEN_QA_FLAGGED)
    assert after == before + 1


def test_lumen_qa_failures_increments() -> None:
    before = _counter_value(LUMEN_QA_FAILURES, reason="timeout")
    LUMEN_QA_FAILURES.labels(reason="timeout").inc()
    after = _counter_value(LUMEN_QA_FAILURES, reason="timeout")
    assert after == before + 1


def test_lumen_sla_breached_increments() -> None:
    before = _counter_value(LUMEN_SLA_BREACHED, priority="high")
    LUMEN_SLA_BREACHED.labels(priority="high").inc()
    after = _counter_value(LUMEN_SLA_BREACHED, priority="high")
    assert after == before + 1


def test_lumen_qa_score_latency_observes() -> None:
    """Histogram accepts observations — Sanity check for the import +
    label config. We don't inspect bucket counts because prometheus_client
    doesn't expose them cheaply; the increment-style tests above cover
    the Counter paths.
    """
    # Should not raise.
    LUMEN_QA_SCORE_LATENCY.observe(0.1)
    LUMEN_QA_SCORE_LATENCY.observe(2.5)