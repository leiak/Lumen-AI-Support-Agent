"""Enums for the Ticket domain.

Seven-state machine (see docs/superpowers/specs/2026-09-18-m2-ai-core-design.md
section 3.2 for the legal transitions) and four priority levels shared by
``Ticket`` and ``SlaPolicy``. Both are stored as Postgres ENUMs (``ticket_status``
/ ``ticket_priority``) created by the alembic migration, so the SQLAlchemy
column declarations use ``create_type=False`` to avoid a second CREATE TYPE
when ``metadata.create_all()`` runs against an already-migrated database.
"""
from enum import StrEnum


class TicketPriority(StrEnum):
    """Priority bucket. Drives default SLA policy lookup."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class TicketStatus(StrEnum):
    """Lifecycle state. Transitions are enforced by the state-machine layer."""

    NEW = "new"
    TRIAGED = "triaged"
    IN_PROGRESS = "in_progress"
    WAITING_CUSTOMER = "waiting_customer"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"