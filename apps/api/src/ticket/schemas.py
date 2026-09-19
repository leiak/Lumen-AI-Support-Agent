"""Pydantic schemas for the Ticket REST API (Task 6).

Three response / request shapes:

* :class:`TicketOut` — the public ticket representation returned by
  ``GET /api/v1/tickets/{id}`` and ``POST /api/v1/tickets/{id}/transition``.
* :class:`TicketTransitionIn` — the body of the transition endpoint.
  ``actor_type`` defaults to ``"agent"`` because the agent workspace is
  the primary caller; admin/system can override.
* :class:`TicketEventOut` — one audit-log row returned by the events
  endpoint.

PII discipline
--------------

``TicketOut.subject`` carries the first 120 chars of the customer
message (see :meth:`ConversationService.record_message` auto-create).
That's PII — only the API contract returns it; we never log it.

All schemas use ``model_config = {"from_attributes": True}`` so they
materialize directly from SQLAlchemy ORM rows (see the repository
methods that return ORM instances).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from ticket.enums import TicketPriority, TicketStatus


class TicketOut(BaseModel):
    """Public ticket representation."""

    id: str
    tenant_id: str
    conversation_id: str
    subject: str
    category: str | None
    priority: TicketPriority
    status: TicketStatus
    assignee_agent_id: str | None
    sla_deadline_at: datetime | None
    first_response_at: datetime | None
    resolved_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TicketTransitionIn(BaseModel):
    """Body of ``POST /api/v1/tickets/{id}/transition``.

    ``to_status`` is the only required field; ``actor_type`` defaults
    to ``"agent"`` because the agent workspace is the primary caller.
    The service stamps the actor_id from the authenticated JWT
    (``claims["sub"]``).
    """

    to_status: TicketStatus
    actor_type: str = "agent"


class TicketEventOut(BaseModel):
    """One audit-log row for a ticket."""

    id: str
    actor_type: str
    actor_id: str | None
    event_type: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


__all__ = ["TicketOut", "TicketTransitionIn", "TicketEventOut"]