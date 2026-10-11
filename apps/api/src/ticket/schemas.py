"""Pydantic schemas for the Ticket REST API (Task 6).

Three response / request shapes:

* :class:`TicketOut` — the public ticket representation returned by
  ``GET /api/v1/tickets/{id}`` and ``POST /api/v1/tickets/{id}/transition``.
* :class:`TicketTransitionIn` — the body of the transition endpoint.
  ``actor_type`` defaults to ``"agent"`` because the agent workspace is
  the primary caller; admin/system can override.
* :class:`TicketEventOut` — one audit-log row returned by the events
  endpoint.

Tier 1 Task 1.1 adds :class:`TicketListRead` (envelope for the admin
ticket-list endpoint: ``{items, total}``).

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

from pydantic import BaseModel, Field

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


class SlaPolicyRead(BaseModel):
    """Public representation of an SLA policy (admin listing endpoint).

    Powers ``GET /api/v1/admin/tenants/{tenant_id}/sla-policies`` — one row
    per priority bucket. Read-only; policies are configured by tenant admins
    out-of-band (seed script / DB migration), so we do not expose create /
    update endpoints here.
    """

    id: str
    name: str
    priority: TicketPriority
    first_response_minutes: int
    resolution_minutes: int
    business_hours_only: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class SlaPolicyCreate(BaseModel):
    """Request body for POST /api/v1/admin/tenants/{tenant_id}/sla-policies.

    Tier 2 Task 2.1 — lets the admin SPA provision SLA policies from
    the UI instead of relying solely on the seed script. ``name`` is
    the natural key (per-tenant uniqueness); ``priority`` is the
    bucket (P0..P3) the SLA applies to.
    """

    name: str = Field(..., min_length=1, max_length=128)
    priority: TicketPriority
    first_response_minutes: int = Field(..., ge=1, le=100_000)
    resolution_minutes: int = Field(..., ge=1, le=1_000_000)
    business_hours_only: bool = False


class SlaPolicyUpdate(BaseModel):
    """Request body for PATCH /api/v1/admin/tenants/{tenant_id}/sla-policies/{policy_id}.

    All fields optional — omitted fields are preserved. ``name`` cannot
    be changed (it is the natural key; to rename, delete + recreate).
    """

    priority: TicketPriority | None = None
    first_response_minutes: int | None = Field(default=None, ge=1, le=100_000)
    resolution_minutes: int | None = Field(default=None, ge=1, le=1_000_000)
    business_hours_only: bool | None = None


class TicketListRead(BaseModel):
    """Envelope for the admin ticket list endpoint (Task 1.1).

    ``total`` is the count of rows matching the filter (ignoring
    limit/offset) so the SPA can render a paginator. Mirrors the
    M4.D budget ``TenantBudgetUsageRead`` envelope shape.
    """

    items: list[TicketOut]
    total: int


__all__ = [
    "SlaPolicyRead",
    "TicketEventOut",
    "TicketListRead",
    "TicketOut",
    "TicketTransitionIn",
]
