"""ORM models for the Ticket domain.

Three independent tables, all tenant-scoped:

* ``tickets`` — one row per support ticket. UNIQUE on ``conversation_id``
  enforces the 1:1 relationship with ``conversations`` at the DB layer
  (a ticket is the source of truth for its conversation's support
  status, not the other way around). Carries SLA deadline plus the
  timestamp trail (first response / resolved / closed).

* ``ticket_events`` — append-only audit log. Every state change, comment,
  assignment, SLA breach, etc. produces one row. ``actor_type`` is one
  of ``system`` / ``agent`` / ``ai`` / ``customer``; ``actor_id`` is
  nullable for system events.

* ``sla_policies`` — tenant-defined SLA rules per priority bucket. The
  service layer looks up the policy matching the ticket's priority at
  create time and stamps ``tickets.sla_deadline_at``.

Indexes:
  * ``idx_tickets_tenant_status`` — list-by-tenant-with-status (workload
    board).
  * ``idx_tickets_assignee`` — partial, only assigned tickets. Saves space
    and lets the assigned-to-me query run on a small index.
  * ``idx_tickets_sla`` — partial on active states only; powers the SLA
    countdown / breach queries.
  * ``idx_ticket_events_ticket`` — tenant-scoped events-per-timeline
    lookup. ``created_at`` is descending in the spec (and the migration
    uses ``DESC``) but SQLAlchemy doesn't enforce index ordering, so we
    leave the column declaration as ASC and rely on Postgres' default
    btree ordering for descending scans.

The Postgres ENUM types ``ticket_priority`` / ``ticket_status`` are created
by the alembic migration. SQLAlchemy uses ``create_type=False`` here so a
later ``Base.metadata.create_all()`` doesn't try to recreate them.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base
from ticket.enums import TicketPriority, TicketStatus


class Ticket(Base):
    """A support ticket. One per Conversation (UNIQUE on conversation_id).

    Status transitions are enforced by the service layer (see Task 5);
    the DB has no CHECK on status so the migration is forward-only.
    SLA deadline + the timestamp trail (first_response_at, resolved_at,
    closed_at) are all nullable — they're set by the service as the
    ticket progresses.

    FKs:
      * ``tenant_id`` → ``tenants.id`` (no ondelete — tenant delete is
        a destructive admin op, not part of the normal flow).
      * ``conversation_id`` → ``conversations.id`` ON DELETE CASCADE —
        deleting a conversation removes its ticket. Ticket is the
        subsidiary record.
      * ``assignee_agent_id`` → ``users.id`` (nullable).
      * ``sla_policy_id`` → ``sla_policies.id`` (nullable, set at create).
    """

    __tablename__ = "tickets"
    __table_args__ = (
        Index("idx_tickets_tenant_status", "tenant_id", "status"),
        Index(
            "idx_tickets_assignee",
            "tenant_id",
            "assignee_agent_id",
            postgresql_where=text("assignee_agent_id IS NOT NULL"),
        ),
        Index(
            "idx_tickets_sla",
            "sla_deadline_at",
            postgresql_where=text(
                "status NOT IN ('resolved', 'closed', 'cancelled')"
            ),
        ),
        UniqueConstraint("conversation_id", name="uq_tickets_conversation_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("tenants.id"),
        nullable=False,
    )
    conversation_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[TicketPriority] = mapped_column(
        SAEnum(TicketPriority, name="ticket_priority", create_type=False),
        nullable=False,
        default=TicketPriority.P2,
    )
    status: Mapped[TicketStatus] = mapped_column(
        SAEnum(TicketStatus, name="ticket_status", create_type=False),
        nullable=False,
        default=TicketStatus.NEW,
    )
    assignee_agent_id: Mapped[str | None] = mapped_column(
        String(26),
        ForeignKey("users.id"),
        nullable=True,
    )
    sla_policy_id: Mapped[str | None] = mapped_column(
        String(26),
        ForeignKey("sla_policies.id"),
        nullable=True,
    )
    sla_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_response_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    events: Mapped[list[TicketEvent]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
    )


class TicketEvent(Base):
    """Append-only audit record for one Ticket.

    Every state change, comment, assignment, priority change, or SLA
    breach produces one row. ``payload`` is JSONB and carries the
    before/after snapshot, comment body, breach reason, etc.
    """

    __tablename__ = "ticket_events"
    __table_args__ = (
        Index("idx_ticket_events_ticket", "tenant_id", "ticket_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("tenants.id"),
        nullable=False,
    )
    ticket_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ticket: Mapped[Ticket] = relationship(back_populates="events")


class SlaPolicy(Base):
    """A tenant-defined SLA rule for a given priority bucket.

    The service layer looks up the policy matching ``Ticket.priority`` at
    create time and stamps ``tickets.sla_deadline_at``. Unique per
    ``(tenant_id, name)`` so tenants can label their policies freely but
    can't collide on names.
    """

    __tablename__ = "sla_policies"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "name", name="uq_sla_policies_tenant_name"
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("tenants.id"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[TicketPriority] = mapped_column(
        SAEnum(TicketPriority, name="ticket_priority", create_type=False),
        nullable=False,
    )
    first_response_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    business_hours_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )