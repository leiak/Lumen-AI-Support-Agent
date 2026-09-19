"""Unit tests for the QA worker (Stage 14 / Task 8).

These tests mock the worker's external boundaries:

* ``JudgeClient.score`` — mocked directly. The Judge internally calls
  ``LLMClient.chat_with_structured_output`` (Task 7 stub); we don't
  mock that layer because the worker never sees it.
* ``Message`` / ``Conversation`` ORM rows — replaced by ``MagicMock``
  instances with the attributes the worker reads (``role``,
  ``conversation_id``, ``content_text``).
* DB session — replaced by an ``AsyncMock`` with the methods the
  worker calls (``session.get``, ``session.add``, ``session.flush``,
  ``session.commit``).

The integration tests (``tests/qa/integration/``) exercise the same
code path against a real Postgres + Redis. The unit tests give us
fast, hermetic coverage of the branching logic without the
infra cost.

Mocking the sessionmaker / async session
---------------------------------------

The worker does ``async with get_sessionmaker()() as session:``.
That syntax means: ``get_sessionmaker()`` returns a sessionmaker;
calling it (i.e. ``sm()``) returns an ``AsyncSession`` which is
itself an async context manager.

For mocking, we provide a fake sessionmaker whose ``__call__``
returns an async-context-manager-style object that yields the same
session on ``__aenter__``. See :func:`_patch_sessionmaker` for the
exact wiring.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conversation.enums import MessageRole
from qa.judge import JudgeFailure, JudgeOutput
from qa.worker import (
    _bucket,
    qa_judge_task,
    qa_sla_alert_worker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session() -> MagicMock:
    """Build a MagicMock that quacks like an AsyncSession for the worker.

    ``__aenter__`` / ``__aexit__`` are configured by the sessionmaker
    patch so the session can be used inside ``async with sm() as
    session:``. We yield the same session instance so per-test
    attribute patches stick across the context-manager boundary.
    """
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    return session


def _patch_sessionmaker(session: MagicMock) -> Any:
    """Patch ``qa.worker.get_sessionmaker`` so its sessionmaker yields ``session``.

    The worker's ``async with sm() as session:`` block requires
    ``sm()`` to return an async context manager. We wire up a thin
    wrapper whose ``__call__`` returns a fresh MagicMock whose
    ``__aenter__`` yields the test-supplied ``session`` — so
    attribute patches on ``session`` persist inside the worker.
    """

    def _sm() -> Any:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    sm = MagicMock(side_effect=_sm)
    return patch("qa.worker.get_sessionmaker", return_value=sm)


def _make_message(
    *,
    message_id: str = "m1",
    role: MessageRole = MessageRole.AI,
    conversation_id: str = "c1",
    content_text: str = "AI answer text",
) -> MagicMock:
    """Build a MagicMock that mimics an ``Message`` ORM row."""
    msg = MagicMock()
    msg.id = message_id
    msg.role = role
    msg.conversation_id = conversation_id
    msg.content_text = content_text
    return msg


def _make_conversation(*, conversation_id: str = "c1", tenant_id: str = "t1") -> MagicMock:
    """Build a MagicMock that mimics a ``Conversation`` ORM row."""
    conv = MagicMock()
    conv.id = conversation_id
    conv.tenant_id = tenant_id
    return conv


def _make_judge(*, output: JudgeOutput | None = None) -> MagicMock:
    """Build a MagicMock JudgeClient with a configurable ``score`` side-effect."""
    judge = MagicMock()
    judge.model = "minimax-m2.7-highspeed"
    judge.is_flagged = MagicMock(return_value=False)
    judge.score = AsyncMock(return_value=output or JudgeOutput(
        relevance=0.9,
        safety=0.95,
        faithfulness=0.85,
        rationale="good answer",
    ))
    return judge


# ---------------------------------------------------------------------------
# _bucket — pure-function smoke tests
# ---------------------------------------------------------------------------


def test_bucket_low_below_0_4() -> None:
    assert _bucket(0.0) == "low"
    assert _bucket(0.39) == "low"


def test_bucket_medium_between_0_4_and_0_7() -> None:
    assert _bucket(0.4) == "medium"
    assert _bucket(0.69) == "medium"


def test_bucket_high_above_0_7() -> None:
    assert _bucket(0.7) == "high"
    assert _bucket(1.0) == "high"


# ---------------------------------------------------------------------------
# qa_judge_task — skip paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qa_judge_skips_when_message_missing() -> None:
    """``session.get(Message, ...)`` returns None → silent skip, no score row."""
    session = _make_session()
    session.get = AsyncMock(return_value=None)

    judge = _make_judge()
    ctx: dict[str, Any] = {"judge_client": judge}

    with _patch_sessionmaker(session):
        await qa_judge_task(ctx, "missing-id")

    # Judge is never called when the message doesn't exist.
    judge.score.assert_not_called()


@pytest.mark.asyncio
async def test_qa_judge_skips_non_ai_role() -> None:
    """A CUSTOMER message → skip (the judge only scores AI turns)."""
    session = _make_session()
    session.get = AsyncMock(
        side_effect=[
            _make_message(role=MessageRole.CUSTOMER),  # Message lookup
            None,  # Conversation lookup never called
        ]
    )

    judge = _make_judge()
    ctx: dict[str, Any] = {"judge_client": judge}

    with _patch_sessionmaker(session):
        await qa_judge_task(ctx, "m-customer")

    judge.score.assert_not_called()


@pytest.mark.asyncio
async def test_qa_judge_skips_duplicate_score() -> None:
    """If a score already exists for this message, skip silently."""
    session = _make_session()
    msg = _make_message()
    conv = _make_conversation()
    session.get = AsyncMock(side_effect=[msg, conv])

    judge = _make_judge()
    ctx: dict[str, Any] = {"judge_client": judge}

    # ``QaScoreRepository.exists_for_message`` returns True — the
    # worker should short-circuit before calling the Judge.
    fake_qa_repo = MagicMock()
    fake_qa_repo.exists_for_message = AsyncMock(return_value=True)
    fake_qa_repo.insert = AsyncMock()

    with _patch_sessionmaker(session), \
         patch("qa.worker.QaScoreRepository", return_value=fake_qa_repo):
        await qa_judge_task(ctx, "m1")

    judge.score.assert_not_called()
    fake_qa_repo.insert.assert_not_called()


@pytest.mark.asyncio
async def test_qa_judge_skips_when_conversation_missing() -> None:
    """Parent Conversation gone → bail (FK should prevent this)."""
    session = _make_session()
    msg = _make_message()
    session.get = AsyncMock(side_effect=[msg, None])  # Message OK, Conv missing

    judge = _make_judge()
    ctx: dict[str, Any] = {"judge_client": judge}

    fake_qa_repo = MagicMock()
    fake_qa_repo.exists_for_message = AsyncMock(return_value=False)

    with _patch_sessionmaker(session), \
         patch("qa.worker.QaScoreRepository", return_value=fake_qa_repo):
        await qa_judge_task(ctx, "m1")

    judge.score.assert_not_called()
    fake_qa_repo.insert.assert_not_called()


# ---------------------------------------------------------------------------
# qa_judge_task — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qa_judge_increments_failures_on_judge_failure() -> None:
    """``JudgeFailure`` → ``LUMEN_QA_FAILURES{reason=judge_failed}.inc()``."""
    session = _make_session()
    msg = _make_message()
    conv = _make_conversation()
    session.get = AsyncMock(side_effect=[msg, conv])

    judge = _make_judge()
    judge.score = AsyncMock(side_effect=JudgeFailure("exhausted"))
    ctx: dict[str, Any] = {"judge_client": judge}

    fake_qa_repo = MagicMock()
    fake_qa_repo.exists_for_message = AsyncMock(return_value=False)
    fake_qa_repo.insert = AsyncMock()

    fake_msg_repo = MagicMock()
    fake_msg_repo.get_last_customer_message = AsyncMock(
        return_value=_make_message(role=MessageRole.CUSTOMER)
    )

    with _patch_sessionmaker(session), \
         patch("qa.worker.QaScoreRepository", return_value=fake_qa_repo), \
         patch("qa.worker.MessageRepository", return_value=fake_msg_repo), \
         patch("qa.worker.LUMEN_QA_FAILURES") as mock_failures, \
         patch("qa.worker.LUMEN_QA_SCORE_LATENCY") as mock_latency:
        mock_inc = MagicMock()
        mock_failures.labels.return_value.inc = mock_inc
        mock_latency.time.return_value = _DummyCM()

        await qa_judge_task(ctx, "m1")

    # Insert must not be called on JudgeFailure.
    fake_qa_repo.insert.assert_not_called()
    # Failure counter should have been bumped with reason=judge_failed.
    mock_failures.labels.assert_called_with(reason="judge_failed")
    mock_inc.assert_called_once()


@pytest.mark.asyncio
async def test_qa_judge_increments_failures_on_unexpected_exception() -> None:
    """Any non-``JudgeFailure`` exception → ``LUMEN_QA_FAILURES{reason=exc_type}``."""
    session = _make_session()
    msg = _make_message()
    conv = _make_conversation()
    session.get = AsyncMock(side_effect=[msg, conv])

    judge = _make_judge()
    judge.score = AsyncMock(side_effect=RuntimeError("network down"))
    ctx: dict[str, Any] = {"judge_client": judge}

    fake_qa_repo = MagicMock()
    fake_qa_repo.exists_for_message = AsyncMock(return_value=False)
    fake_qa_repo.insert = AsyncMock()

    fake_msg_repo = MagicMock()
    fake_msg_repo.get_last_customer_message = AsyncMock(
        return_value=_make_message(role=MessageRole.CUSTOMER)
    )

    with _patch_sessionmaker(session), \
         patch("qa.worker.QaScoreRepository", return_value=fake_qa_repo), \
         patch("qa.worker.MessageRepository", return_value=fake_msg_repo), \
         patch("qa.worker.LUMEN_QA_FAILURES") as mock_failures, \
         patch("qa.worker.LUMEN_QA_SCORE_LATENCY") as mock_latency:
        mock_failures.labels.return_value.inc = MagicMock()
        mock_latency.time.return_value = _DummyCM()

        await qa_judge_task(ctx, "m1")

    fake_qa_repo.insert.assert_not_called()
    mock_failures.labels.assert_called_with(reason="RuntimeError")


# ---------------------------------------------------------------------------
# qa_judge_task — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qa_judge_writes_score_and_increments_metrics_on_success() -> None:
    """Happy path: insert + 4 ``LUMEN_QA_SCORES`` increments + no flagged."""
    session = _make_session()
    msg = _make_message()
    conv = _make_conversation()
    session.get = AsyncMock(side_effect=[msg, conv])

    judge_output = JudgeOutput(
        relevance=0.9,
        safety=0.95,
        faithfulness=0.85,
        rationale="good answer",
    )
    judge = _make_judge(output=judge_output)
    judge.is_flagged = MagicMock(return_value=False)
    ctx: dict[str, Any] = {"judge_client": judge}

    fake_qa_repo = MagicMock()
    fake_qa_repo.exists_for_message = AsyncMock(return_value=False)
    fake_qa_repo.insert = AsyncMock()

    fake_msg_repo = MagicMock()
    fake_msg_repo.get_last_customer_message = AsyncMock(
        return_value=_make_message(role=MessageRole.CUSTOMER)
    )

    with _patch_sessionmaker(session), \
         patch("qa.worker.QaScoreRepository", return_value=fake_qa_repo), \
         patch("qa.worker.MessageRepository", return_value=fake_msg_repo), \
         patch("qa.worker.LUMEN_QA_SCORES") as mock_scores, \
         patch("qa.worker.LUMEN_QA_FLAGGED") as mock_flagged, \
         patch("qa.worker.LUMEN_QA_SCORE_LATENCY") as mock_latency:
        # Histogram context manager — return a no-op.
        mock_latency.time.return_value = _DummyCM()

        await qa_judge_task(ctx, "m1")

    # Insert called with the expected parameters.
    fake_qa_repo.insert.assert_awaited_once()
    insert_kwargs = fake_qa_repo.insert.await_args.kwargs
    assert insert_kwargs["tenant_id"] == "t1"
    assert insert_kwargs["message_id"] == "m1"
    assert insert_kwargs["judge_model"] == "minimax-m2.7-highspeed"
    assert insert_kwargs["relevance"] == 0.9
    assert insert_kwargs["safety"] == 0.95
    assert insert_kwargs["faithfulness"] == 0.85
    assert insert_kwargs["flagged"] is False

    # Four LUMEN_QA_SCORES increments (relevance, safety, faithfulness, overall).
    assert mock_scores.labels.call_count == 4
    # No flagged increment because is_flagged returned False.
    mock_flagged.inc.assert_not_called()
    # Commit fires at the end.
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_qa_judge_increments_flagged_when_below_threshold() -> None:
    """Low scores → ``judge.is_flagged`` returns True → ``LUMEN_QA_FLAGGED.inc()``."""
    session = _make_session()
    msg = _make_message()
    conv = _make_conversation()
    session.get = AsyncMock(side_effect=[msg, conv])

    judge_output = JudgeOutput(
        relevance=0.1,
        safety=0.1,
        faithfulness=0.1,
        rationale="bad",
    )
    judge = _make_judge(output=judge_output)
    judge.is_flagged = MagicMock(return_value=True)
    ctx: dict[str, Any] = {"judge_client": judge}

    fake_qa_repo = MagicMock()
    fake_qa_repo.exists_for_message = AsyncMock(return_value=False)
    fake_qa_repo.insert = AsyncMock()

    fake_msg_repo = MagicMock()
    fake_msg_repo.get_last_customer_message = AsyncMock(
        return_value=_make_message(role=MessageRole.CUSTOMER)
    )

    with _patch_sessionmaker(session), \
         patch("qa.worker.QaScoreRepository", return_value=fake_qa_repo), \
         patch("qa.worker.MessageRepository", return_value=fake_msg_repo), \
         patch("qa.worker.LUMEN_QA_SCORES") as mock_scores, \
         patch("qa.worker.LUMEN_QA_FLAGGED") as mock_flagged, \
         patch("qa.worker.LUMEN_QA_SCORE_LATENCY") as mock_latency:
        mock_latency.time.return_value = _DummyCM()
        mock_scores.labels.return_value.inc = MagicMock()

        await qa_judge_task(ctx, "m1")

    # Flagged counter incremented once.
    mock_flagged.inc.assert_called_once()
    insert_kwargs = fake_qa_repo.insert.await_args.kwargs
    assert insert_kwargs["flagged"] is True


@pytest.mark.asyncio
async def test_qa_judge_uses_empty_question_when_no_customer_message() -> None:
    """Edge case: AI message with no preceding customer turn → empty question."""
    session = _make_session()
    msg = _make_message()
    conv = _make_conversation()
    session.get = AsyncMock(side_effect=[msg, conv])

    judge = _make_judge()
    ctx: dict[str, Any] = {"judge_client": judge}

    fake_qa_repo = MagicMock()
    fake_qa_repo.exists_for_message = AsyncMock(return_value=False)
    fake_qa_repo.insert = AsyncMock()

    fake_msg_repo = MagicMock()
    fake_msg_repo.get_last_customer_message = AsyncMock(return_value=None)

    with _patch_sessionmaker(session), \
         patch("qa.worker.QaScoreRepository", return_value=fake_qa_repo), \
         patch("qa.worker.MessageRepository", return_value=fake_msg_repo), \
         patch("qa.worker.LUMEN_QA_SCORE_LATENCY") as mock_latency:
        mock_latency.time.return_value = _DummyCM()

        await qa_judge_task(ctx, "m1")

    # Judge still called with empty question.
    judge_input = judge.score.await_args.args[0]
    assert judge_input.question == ""


# ---------------------------------------------------------------------------
# qa_sla_alert_worker — smoke test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qa_sla_alert_worker_increments_per_priority() -> None:
    """``LUMEN_SLA_BREACHED{priority}`` bumped for each breached ticket."""
    # Two breached tickets: P0 + P2.
    t0 = MagicMock()
    t0.id = "t_a"
    t0.priority = MagicMock()
    t0.priority.value = "P0"

    t2 = MagicMock()
    t2.id = "t_b"
    t2.priority = MagicMock()
    t2.priority.value = "P2"

    session = _make_session()
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=[t0, t2])
    exec_result = MagicMock()
    exec_result.scalars = MagicMock(return_value=scalars_mock)

    async def _fake_execute(*_args: Any, **_kwargs: Any) -> Any:
        return exec_result

    session.execute = _fake_execute

    with _patch_sessionmaker(session), \
         patch("qa.worker.LUMEN_SLA_BREACHED") as mock_sla:
        mock_sla.labels.return_value.inc = MagicMock()
        await qa_sla_alert_worker(ctx={"judge_client": MagicMock()})

    # Two labels() calls: one for P0, one for P2.
    assert mock_sla.labels.call_count == 2
    mock_sla.labels.assert_any_call(priority="P0")
    mock_sla.labels.assert_any_call(priority="P2")


@pytest.mark.asyncio
async def test_qa_sla_alert_worker_no_op_when_no_breaches() -> None:
    """Empty result → no ``LUMEN_SLA_BREACHED`` increments."""
    session = _make_session()
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=[])
    exec_result = MagicMock()
    exec_result.scalars = MagicMock(return_value=scalars_mock)

    async def _fake_execute(*_args: Any, **_kwargs: Any) -> Any:
        return exec_result

    session.execute = _fake_execute

    with _patch_sessionmaker(session), \
         patch("qa.worker.LUMEN_SLA_BREACHED") as mock_sla:
        mock_sla.labels.return_value.inc = MagicMock()
        await qa_sla_alert_worker(ctx={"judge_client": MagicMock()})

    mock_sla.labels.assert_not_called()


# ---------------------------------------------------------------------------
# WorkerSettings shape
# ---------------------------------------------------------------------------


def test_worker_settings_registers_qa_judge_task() -> None:
    """``WorkerSettings.functions`` lists ``qa_judge_task`` and ``functions`` is a list."""
    from qa.worker import WorkerSettings

    assert qa_judge_task in WorkerSettings.functions
    assert isinstance(WorkerSettings.functions, list)


def test_worker_settings_cron_includes_sla_worker() -> None:
    """``WorkerSettings.cron_jobs`` has one entry for the SLA scan."""
    from qa.worker import WorkerSettings

    assert len(WorkerSettings.cron_jobs) == 1


def test_worker_settings_max_jobs_is_io_bound_friendly() -> None:
    """``max_jobs=4`` — IO-bound judge, 4 concurrent is plenty."""
    from qa.worker import WorkerSettings

    assert WorkerSettings.max_jobs == 4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _DummyCM:
    """No-op context manager for ``Histogram.time()`` mocks."""

    def __enter__(self) -> "_DummyCM":
        return self

    def __exit__(self, *args: Any) -> None:
        return None