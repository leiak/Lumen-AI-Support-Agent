"""Repository layer for ``MessageQaScore`` rows.

Stage 14 / Task 8 — the worker writes one row per scored AI message.
The repository is tenant-scoped at every read/write (Stage 13 lesson:
tenant_id is the security boundary, not a soft filter) and the
``insert`` method flushes but does NOT commit — the worker owns the
outer transaction so a failed metrics write can roll back together
with the score row.

Public surface
--------------

* ``exists_for_message`` — idempotency short-circuit (also backed by
  the ``uq_qa_scores_message`` UNIQUE constraint at the DB level).
* ``insert`` — flushes the new row; caller commits.
* ``get_for_message`` — used by integration tests + future admin
  endpoints.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.id_gen import new_id
from qa.models import MessageQaScore


class QaScoreRepository:
    """CRUD for ``MessageQaScore`` rows. Always tenant-scoped.

    Constructed with an externally-owned :class:`AsyncSession` so the
    caller controls transaction boundaries (``commit`` / ``rollback``).
    The repository NEVER commits on its own — every method flushes at
    most, leaving the surrounding transaction intact.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def exists_for_message(
        self, *, message_id: str, tenant_id: str
    ) -> bool:
        """Return True iff a score row already exists for this message.

        Backs the worker's idempotency check (``uq_qa_scores_message``
        is the DB-level backstop; this is the cheap short-circuit so a
        retried job doesn't re-judge an already-scored turn).

        Tenant-scoped: a score from a different tenant never matches,
        which makes the worker safe against accidental cross-tenant
        reuse of an ULID.
        """
        result = await self.session.execute(
            select(MessageQaScore.id)
            .where(
                MessageQaScore.message_id == message_id,
                MessageQaScore.tenant_id == tenant_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def insert(
        self,
        *,
        tenant_id: str,
        message_id: str,
        judge_model: str,
        relevance: float,
        safety: float,
        faithfulness: float,
        overall: float,
        rationale: str | None,
        flagged: bool,
    ) -> MessageQaScore:
        """Insert a new score row. Flushes; caller commits.

        ``rationale`` is the Judge's free-text explanation; the
        ``MessageQaScore.rationale`` column accepts ``None`` so the
        caller may pass ``None`` to omit it. The ``MessageQaScore``
        ORM model has no Python-side validator on rationale length;
        the Judge client (300-char Pydantic cap) is the source of
        truth for that invariant.
        """
        score = MessageQaScore(
            id=new_id(),
            tenant_id=tenant_id,
            message_id=message_id,
            judge_model=judge_model,
            relevance_score=relevance,
            safety_score=safety,
            faithfulness_score=faithfulness,
            overall_score=overall,
            rationale=rationale,
            flagged=flagged,
        )
        self.session.add(score)
        await self.session.flush()
        return score

    async def get_for_message(
        self, *, message_id: str, tenant_id: str
    ) -> MessageQaScore | None:
        """Look up a single score row by message id (tenant-scoped).

        Returns ``None`` for not-found OR for cross-tenant rows — the
        API layer maps both cases to 404 to prevent tenant
        enumeration via response-shape differences.
        """
        result = await self.session.execute(
            select(MessageQaScore).where(
                MessageQaScore.message_id == message_id,
                MessageQaScore.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()


__all__ = ["QaScoreRepository"]