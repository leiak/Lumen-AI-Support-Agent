"""Arq worker for the QA judging subsystem.

Stage 14 / Task 8 — wires the Judge client from Task 7 into a real
background-worker process and exposes two registered functions:

* :func:`qa_judge_task` — enqueued by ``channel.inbound`` after every
  AI message persist. Loads the message, runs the Judge, persists a
  ``MessageQaScore`` row, and bumps the four ``LUMEN_QA_*`` metrics.
* :func:`qa_sla_alert_worker` — hourly cron. Scans open tickets
  whose ``sla_deadline_at`` is in the past and bumps
  ``LUMEN_SLA_BREACHED{priority}``. M2.A wires the metric; the
  PagerDuty / Slack routing is M3.

Lifecycle hooks
---------------

* ``startup(ctx)`` — built once per worker process. Constructs a
  :class:`JudgeClient` from settings, registers it in ``ctx`` for
  the ``qa_judge_task`` function to reuse.
* ``shutdown(ctx)`` — closes the Judge's underlying LLM HTTP client
  so the arq process doesn't leak sockets on shutdown.

Design constraints
------------------

* **PII safety.** Worker logs carry opaque IDs (message_id, tenant_id)
  only — never the AI text, the customer question, the Judge's
  rationale, or any PII from the conversation.
* **Multi-tenant isolation.** ``QaScoreRepository`` is tenant-scoped
  on every read/write. The worker's tenant_id comes from the loaded
  ``Message`` row's parent ``Conversation`` (Message itself doesn't
  carry tenant_id; Conversation does).
* **Best-effort scoring.** A judge failure (``JudgeFailure`` from
  retries-exhausted, or any other exception) increments
  ``LUMEN_QA_FAILURES`` and returns cleanly. The AI message is the
  product; a judge failure must not retry-flood the worker queue.
  ``max_retries=1`` (default) means a single bad turn costs at most
  2 LLM calls.
* **Worker idempotency.** The composite UNIQUE on
  ``message_qa_scores.message_id`` plus the
  ``exists_for_message`` short-circuit make the worker safe to
  retry. A retried job on an already-scored message is a silent
  no-op.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from arq import cron
from arq.connections import ArqRedis
from sqlalchemy import select

from conversation.enums import MessageRole
from conversation.models import Conversation, Message
from conversation.repository import MessageRepository
from core.business_metrics import (
    LUMEN_QA_FAILURES,
    LUMEN_QA_FLAGGED,
    LUMEN_QA_SCORE_LATENCY,
    LUMEN_QA_SCORES,
    LUMEN_SLA_BREACHED,
)
from core.config import get_settings
from core.database import get_sessionmaker
from core.logging import get_logger
from qa.judge import JudgeClient, JudgeFailure, JudgeInput
from qa.repository import QaScoreRepository
from ticket.enums import TicketStatus
from ticket.models import Ticket

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Score bucket boundaries
# ---------------------------------------------------------------------------
#
# Three buckets keep ``LUMEN_QA_SCORES{dimension, bucket}`` cardinality
# bounded at 3 dimensions × 3 buckets = 9 series (well under Prometheus'
# 100k cap). Boundaries are inclusive on the lower side and exclusive on
# the upper side, matching the convention used by Stage 10's RAG eval
# suite.
def _bucket(score: float) -> str:
    """Map a 0-1 score into ``low`` / ``medium`` / ``high``.

    * ``score < 0.4`` → ``low``
    * ``0.4 <= score < 0.7`` → ``medium``
    * ``score >= 0.7`` → ``high``

    Pure function — no I/O, no logging. Cheap to call from the metric
    increment site.
    """
    if score < 0.4:
        return "low"
    if score < 0.7:
        return "medium"
    return "high"


# ---------------------------------------------------------------------------
# Arq task: judge a single AI message
# ---------------------------------------------------------------------------


async def qa_judge_task(ctx: dict[str, Any], message_id: str) -> None:
    """Score one AI message. Idempotent: re-running is a silent no-op.

    The function is the worker's boundary; integration tests mock
    ``JudgeClient.score`` directly and let everything else run for
    real. The Judge internally calls
    ``LLMClient.chat_with_structured_output`` which is stubbed in
    Task 7 — but the worker doesn't see that layer at all.

    Algorithm
    ---------

    1. Open a session via the project sessionmaker.
    2. Load the ``Message`` by primary key. Skip if it doesn't
       exist or isn't role=AI.
    3. Load the parent ``Conversation`` for the tenant_id (Message
       has no tenant_id column).
    4. Skip if a ``MessageQaScore`` already exists for this
       ``message_id`` (idempotency).
    5. Load the most recent CUSTOMER message in the conversation —
       that's the "question" half of the ``(question, answer)``
       pair. If there's no customer message yet (edge case), use an
       empty string.
    6. Call ``JudgeClient.score`` under the
       ``LUMEN_QA_SCORE_LATENCY`` histogram timer.
    7. On ``JudgeFailure`` → inc
       ``LUMEN_QA_FAILURES{reason=judge_failed}`` and return.
    8. On any other exception → inc
       ``LUMEN_QA_FAILURES{reason=exc_type}`` and return.
    9. Compute ``overall`` = mean of the three dimensions, insert
       the ``MessageQaScore`` row, inc the four
       ``LUMEN_QA_SCORES{dimension, bucket}`` counters.
    10. If ``judge.is_flagged(output)`` → inc ``LUMEN_QA_FLAGGED``
        and log at WARNING with opaque IDs only (NEVER log the
        rationale — it may quote the customer's question or the AI's
        answer).

    The ``rationale`` is PII-adjacent (the Judge's free-text
    explanation may quote the conversation turn) — it goes to the DB
    row for admin audit, NOT to the worker logs.
    """
    judge: JudgeClient = ctx["judge_client"]

    sm = get_sessionmaker()
    async with sm() as session:
        msg = await session.get(Message, message_id)
        if msg is None:
            log.info(
                "qa.judge.message_missing",
                message_id=message_id,
            )
            return
        if msg.role != MessageRole.AI:
            role_label = (
                msg.role.value
                if hasattr(msg.role, "value")
                else str(msg.role)
            )
            log.info(
                "qa.judge.skip_non_ai",
                message_id=message_id,
                role=role_label,
            )
            return

        # Conversation.tenant_id is the tenant boundary for this
        # message (Message itself has no tenant_id column). Pull it
        # here so we can thread it into the repository + Judge
        # context.
        conv = await session.get(Conversation, msg.conversation_id)
        if conv is None:
            # FK should prevent this; if it ever happens the work is
            # unrecoverable. Log and bail.
            log.error(
                "qa.judge.conversation_missing",
                message_id=message_id,
                conversation_id=msg.conversation_id,
            )
            return
        tenant_id = conv.tenant_id

        qa_repo = QaScoreRepository(session)
        if await qa_repo.exists_for_message(
            message_id=message_id, tenant_id=tenant_id
        ):
            log.info(
                "qa.judge.skip_duplicate",
                message_id=message_id,
            )
            return

        # Last customer message — the "question" the AI replied to.
        # Uses a fresh session via the MessageRepository helper to
        # keep the read consistent with the worker's outer
        # transaction (the helper opens its own session).
        msg_repo = MessageRepository()
        last_customer = await msg_repo.get_last_customer_message(
            conversation_id=msg.conversation_id,
            tenant_id=tenant_id,
        )
        question = (last_customer.content_text if last_customer else "") or ""

        # ``Message`` has no ``citations`` column in M1 — the
        # RAG-retrieved article IDs travel via ``tool_calls_json`` and
        # will be wired in M2.A Task 12. For Task 8 we pass an empty
        # list so the Judge runs on (question, answer) only.
        judge_input = JudgeInput(
            question=question,
            answer=msg.content_text,
            citations=[],
        )

        try:
            with LUMEN_QA_SCORE_LATENCY.time():
                output = await judge.score(judge_input)
        except JudgeFailure as exc:
            LUMEN_QA_FAILURES.labels(reason="judge_failed").inc()
            log.warning(
                "qa.judge.failure",
                message_id=message_id,
                error_type=type(exc).__name__,
            )
            return
        except Exception as exc:  # noqa: BLE001 — best-effort worker
            LUMEN_QA_FAILURES.labels(reason=type(exc).__name__).inc()
            log.warning(
                "qa.judge.unexpected_error",
                message_id=message_id,
                error_type=type(exc).__name__,
            )
            return

        overall = (output.relevance + output.safety + output.faithfulness) / 3.0
        flagged = judge.is_flagged(output)

        await qa_repo.insert(
            tenant_id=tenant_id,
            message_id=message_id,
            judge_model=judge.model,
            relevance=output.relevance,
            safety=output.safety,
            faithfulness=output.faithfulness,
            overall=overall,
            rationale=output.rationale,
            flagged=flagged,
        )

        for dim, val in (
            ("relevance", output.relevance),
            ("safety", output.safety),
            ("faithfulness", output.faithfulness),
            ("overall", overall),
        ):
            LUMEN_QA_SCORES.labels(dimension=dim, bucket=_bucket(val)).inc()

        if flagged:
            LUMEN_QA_FLAGGED.inc()
            # Opaque IDs only — the rationale (and the question /
            # answer it was scored against) is PII-adjacent and must
            # never appear in worker logs.
            log.warning(
                "qa.judge.flagged",
                message_id=message_id,
                judge_model=judge.model,
            )

        await session.commit()


# ---------------------------------------------------------------------------
# Arq cron: hourly SLA-breach scan
# ---------------------------------------------------------------------------


async def qa_sla_alert_worker(ctx: dict[str, Any]) -> None:
    """Scan open tickets past ``sla_deadline_at`` → inc ``LUMEN_SLA_BREACHED``.

    Cron: registered at minute={0} (top of every hour). M2.A doesn't
    poll every minute — the hourly cadence keeps the metric
    observability story coherent with M3's eventual PagerDuty wiring
    (which itself doesn't need sub-hourly granularity).

    Open states match the ticket state machine (Task 4): NEW,
    TRIAGED, IN_PROGRESS, WAITING_CUSTOMER. RESOLVED / CLOSED /
    CANCELLED are excluded — they don't have a live SLA.

    ``priority`` is a ``TicketPriority`` StrEnum; the
    ``.value`` (e.g. ``"P0"``) is what we label the counter with.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        now = datetime.now(UTC)
        stmt = select(Ticket).where(
            Ticket.sla_deadline_at.is_not(None),
            Ticket.sla_deadline_at < now,
            Ticket.status.in_(
                [
                    TicketStatus.NEW,
                    TicketStatus.TRIAGED,
                    TicketStatus.IN_PROGRESS,
                    TicketStatus.WAITING_CUSTOMER,
                ]
            ),
        )
        result = await session.execute(stmt)
        breached = list(result.scalars().all())

    for ticket in breached:
        priority_label = (
            ticket.priority.value
            if hasattr(ticket.priority, "value")
            else str(ticket.priority)
        )
        LUMEN_SLA_BREACHED.labels(priority=priority_label).inc()
        log.warning(
            "qa.sla.breach",
            ticket_id=ticket.id,
            priority=priority_label,
        )


# ---------------------------------------------------------------------------
# Worker lifecycle hooks
# ---------------------------------------------------------------------------


async def startup(ctx: dict[str, Any]) -> None:
    """Build the :class:`JudgeClient` once per worker process.

    Registered as the Arq ``on_startup`` hook. The Judge client is
    heavy (it builds an ``LLMClient`` + provider + retry policy); we
    build it once and stash it in ``ctx`` for the task functions to
    reuse. The Judge's model name is logged at INFO so operators can
    verify the worker booted with the right provider / model.
    """
    ctx["judge_client"] = JudgeClient.from_settings()
    judge = ctx["judge_client"]
    log.info(
        "qa.worker.startup",
        judge_model=judge.model,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    """Close the Judge's HTTP client on worker shutdown.

    The ``JudgeClient`` holds an ``LLMClient`` which holds a
    provider-specific HTTPX pool (OpenAI/Anthropic). Without this
    hook, arq's process exit would leak the pool. ``aclose`` is a
    no-op for providers that don't expose an HTTPX client.
    """
    judge: JudgeClient | None = ctx.get("judge_client")
    if judge is not None:
        try:
            await judge.llm.aclose()
        except Exception as exc:  # noqa: BLE001 — shutdown is best-effort
            log.warning(
                "qa.worker.shutdown_aclose_failed",
                error_type=type(exc).__name__,
            )
    log.info("qa.worker.shutdown")


# ---------------------------------------------------------------------------
# Arq WorkerSettings
# ---------------------------------------------------------------------------


class WorkerSettings:
    """Arq ``WorkerSettings`` — exposed via ``apps.api.src.workers``.

    Invoked by the production command::

        arq apps.api.src.workers.WorkerSettings

    Registered functions:

    * :func:`qa_judge_task` — main workload, enqueued per AI message.
    * :func:`qa_sla_alert_worker` — hourly cron, scans breached SLAs.

    ``max_jobs=4``: the Judge is IO-bound (HTTP roundtrip to the
    LLM provider); 4 concurrent jobs is enough headroom without
    saturating the Judge provider's rate limit. Tunable per
    environment in a future iteration.
    """

    functions = [qa_judge_task]
    cron_jobs = [cron(qa_sla_alert_worker, minute={0})]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 4


# ---------------------------------------------------------------------------
# Arq Redis helper (used by channel.inbound to enqueue judge tasks)
# ---------------------------------------------------------------------------


def build_arq_redis() -> ArqRedis:
    """Build an :class:`ArqRedis` client from app settings.

    Used by :func:`apps.api.src.channel.inbound._enqueue_qa_judge` to
    enqueue ``qa_judge_task`` jobs WITHOUT spinning up the full
    WorkerSettings (which would block the inbound path).

    ``ArqRedis.from_url`` parses the ``redis_url`` setting into a
    real connection pool. The helper does NOT connect — ArqRedis
    connects lazily on first command, matching the ``core.redis``
    pool's behaviour.
    """
    settings = get_settings()
    return ArqRedis.from_url(settings.redis_url)


__all__ = [
    "WorkerSettings",
    "build_arq_redis",
    "qa_judge_task",
    "qa_sla_alert_worker",
]