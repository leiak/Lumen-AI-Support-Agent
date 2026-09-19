"""Integration tests for the QA worker (Stage 14 / Task 8).

Exercises ``qa_judge_task`` end-to-end against a real Postgres
session. The Judge LLM is mocked at the ``JudgeClient.score``
boundary so the suite doesn't need a live LLM provider.

Marked ``@pytest.mark.integration`` — these tests need a live
Postgres. The default ``-m "not integration"`` invocation skips
them; run with ``-m integration`` to include.

PII discipline
--------------

The customer / AI messages carry distinctive
``MAGIC_PHRASE_QA_*`` markers so the worker logs and assertions
never depend on real customer text. We never read the actual
``content_text`` of the seeded messages in this file — only the
shape (id, role, score row existence).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qa.judge import JudgeOutput
from qa.models import MessageQaScore
from qa.worker import qa_judge_task


def _make_judge(output: JudgeOutput, *, flagged: bool) -> MagicMock:
    """Build a MagicMock JudgeClient with the desired output and flagged-state."""
    judge = MagicMock()
    judge.model = "minimax-m2.7-highspeed"
    judge.is_flagged = MagicMock(return_value=flagged)
    judge.score = AsyncMock(return_value=output)
    return judge


@pytest.mark.integration
@pytest.mark.asyncio
async def test_qa_worker_scores_ai_message(
    sample_ai_message: Any,
    db_session: Any,
) -> None:
    """Happy path: ``qa_judge_task`` writes a ``MessageQaScore`` row."""
    judge_output = JudgeOutput(
        relevance=0.9,
        safety=0.95,
        faithfulness=0.85,
        rationale="MAGIC_PHRASE_QA_GOOD_004",
    )
    judge = _make_judge(judge_output, flagged=False)
    ctx: dict[str, Any] = {"judge_client": judge}

    await qa_judge_task(ctx, sample_ai_message.id)

    # Score row exists for the message.
    score = (
        await db_session.execute(
            MessageQaScore.__table__.select().where(
                MessageQaScore.message_id == sample_ai_message.id
            )
        )
    ).first()
    assert score is not None
    # Overall = mean of 0.9 / 0.95 / 0.85 = 0.9.
    assert score.relevance_score == 0.9
    assert score.safety_score == 0.95
    assert score.faithfulness_score == 0.85
    assert score.flagged is False
    # Judge was invoked exactly once.
    judge.score.assert_awaited_once()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_qa_worker_skips_non_ai_message(
    sample_human_message: Any,
    db_session: Any,
) -> None:
    """A CUSTOMER message → silent skip, no ``MessageQaScore`` written."""
    judge = MagicMock()
    judge.model = "minimax-m2.7-highspeed"
    ctx: dict[str, Any] = {"judge_client": judge}

    await qa_judge_task(ctx, sample_human_message.id)

    # No score row exists for this customer message.
    score = (
        await db_session.execute(
            MessageQaScore.__table__.select().where(
                MessageQaScore.message_id == sample_human_message.id
            )
        )
    ).first()
    assert score is None
    # Judge was NOT called — the worker short-circuits on role.
    judge.score.assert_not_called()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_qa_worker_is_idempotent(
    sample_ai_message: Any,
    sample_existing_qa_score: MessageQaScore,
    db_session: Any,
) -> None:
    """A second call against an already-scored message is a silent no-op.

    Backed by the ``uq_qa_scores_message`` UNIQUE constraint at the
    DB layer and the ``exists_for_message`` short-circuit in the
    repository layer. The second call must NOT overwrite the
    existing row.
    """
    judge_output = JudgeOutput(
        relevance=0.8,
        safety=0.8,
        faithfulness=0.8,
        rationale="MAGIC_PHRASE_QA_DUP_005",
    )
    judge = _make_judge(judge_output, flagged=False)
    ctx: dict[str, Any] = {"judge_client": judge}

    await qa_judge_task(ctx, sample_ai_message.id)

    # Judge was NOT called — exists_for_message short-circuited.
    judge.score.assert_not_called()

    # The pre-existing score row is untouched — its rationale still
    # carries the original marker, not the would-be duplicate's.
    existing = (
        await db_session.execute(
            MessageQaScore.__table__.select().where(
                MessageQaScore.message_id == sample_ai_message.id
            )
        )
    ).first()
    assert existing is not None
    assert existing.rationale == "MAGIC_PHRASE_QA_EXISTING_003"
    assert existing.relevance_score == 0.5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_qa_worker_increments_metrics_on_flagged_score(
    sample_ai_message: Any,
    db_session: Any,
) -> None:
    """A flagged score → ``LUMEN_QA_FLAGGED.inc()`` observed by the test."""
    from core.business_metrics import LUMEN_QA_FLAGGED

    def _current() -> float:
        for metric in LUMEN_QA_FLAGGED.collect():
            for sample in metric.samples:
                if sample.name.endswith("_total"):
                    return sample.value
        return 0.0

    judge_output = JudgeOutput(
        relevance=0.1,
        safety=0.1,
        faithfulness=0.1,
        rationale="MAGIC_PHRASE_QA_BAD_006",
    )
    judge = _make_judge(judge_output, flagged=True)
    ctx: dict[str, Any] = {"judge_client": judge}

    before = _current()
    await qa_judge_task(ctx, sample_ai_message.id)
    after = _current()

    assert after == before + 1

    # The persisted row carries ``flagged=True``.
    score = (
        await db_session.execute(
            MessageQaScore.__table__.select().where(
                MessageQaScore.message_id == sample_ai_message.id
            )
        )
    ).first()
    assert score is not None
    assert score.flagged is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_qa_worker_writes_overall_score(
    sample_ai_message: Any,
    db_session: Any,
) -> None:
    """``overall_score`` is the mean of the three dimensions."""
    judge_output = JudgeOutput(
        relevance=0.6,
        safety=0.9,
        faithfulness=0.3,
        rationale="MAGIC_PHRASE_QA_OVERALL_007",
    )
    judge = _make_judge(judge_output, flagged=False)
    ctx: dict[str, Any] = {"judge_client": judge}

    await qa_judge_task(ctx, sample_ai_message.id)

    score = (
        await db_session.execute(
            MessageQaScore.__table__.select().where(
                MessageQaScore.message_id == sample_ai_message.id
            )
        )
    ).first()
    assert score is not None
    # (0.6 + 0.9 + 0.3) / 3 = 0.6
    assert abs(score.overall_score - 0.6) < 1e-9
    # The relevance/safety/faithfulness values are stored verbatim.
    assert score.relevance_score == 0.6
    assert score.safety_score == 0.9
    assert score.faithfulness_score == 0.3