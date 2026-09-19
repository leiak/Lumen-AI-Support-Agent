"""ORM models for real-time QA scoring.

Stage 14 / Task 7 — every AI message is judged by a small LLM in an
Arq background task; the score is persisted here so admins can audit
flagged turns.

Design choices
--------------

* **Score columns are ``Float`` (not ``Numeric``).** This matches the
  JudgeOutput Pydantic schema (``float``, ``ge=0``, ``le=1``) and keeps
  the JSON-shaped API surface consistent with the DB shape. A 0.01
  precision loss is irrelevant for a 0-1 QA signal and avoids the
  Decimal <-> float friction you'd hit in FastAPI responses.

* **Composite unique on ``message_id``** (``uq_qa_scores_message``)
  prevents accidental double-scoring if the worker retries after a
  transient error. The repository layer's ``exists_for_message`` is the
  short-circuit; this constraint is the backstop.

* **Composite index ``(tenant_id, created_at DESC)``** powers the admin
  "recent flagged" view in M2.A. We avoid naming it with DESC in the
  index itself because Postgres can scan either direction cheaply;
  the operator just hints the planner.

* **No RLS dependency.** Tenant isolation is enforced by the
  repository layer (every read/write filters by ``tenant_id``); the
  table itself has ``tenant_id`` as a plain column. This matches the
  M1 ``Message`` and ``Conversation`` models.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class MessageQaScore(Base):
    __tablename__ = "message_qa_scores"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    judge_model: Mapped[str] = mapped_column(String(100), nullable=False)
    # Scores are floats in [0, 1]. Pydantic enforces the range on
    # JudgeOutput; this Float column stores whatever the JudgeClient
    # persisted (no DB-side CHECK because migrations are simpler
    # without one — the worker is the single writer).
    relevance_score: Mapped[float] = mapped_column(nullable=False)
    safety_score: Mapped[float] = mapped_column(nullable=False)
    faithfulness_score: Mapped[float] = mapped_column(nullable=False)
    overall_score: Mapped[float] = mapped_column(nullable=False)
    # Rationale is bounded to 300 chars at the JudgeOutput (Pydantic)
    # layer. We leave it as unbounded ``Text`` at the DB level for
    # defense-in-depth — the Pydantic constraint is the source of
    # truth, and ``Text`` lets us bump the limit later without a
    # migration.
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("message_id", name="uq_qa_scores_message"),
        Index(
            "ix_message_qa_scores_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )