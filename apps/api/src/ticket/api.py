"""Ticket REST API (Task 6, M2.A).

All routes under ``/api/v1/tickets``, all behind JWT auth. Tenant
context is taken from the JWT ``tenant_id`` claim — there is no way
for a caller to act on a different tenant's ticket.

Endpoints
---------

* ``GET    /tickets/{ticket_id}`` — fetch a single ticket. 404 on
  missing or cross-tenant (anti-enumeration).
* ``POST   /tickets/{ticket_id}/transition`` — drive the state
  machine. ``409 Conflict`` (not 400) on an illegal transition —
  400 is for malformed requests, 409 is for state conflicts.
* ``GET    /tickets/{ticket_id}/events`` — list audit-log rows for
  the ticket, newest first. 404 on missing / cross-tenant ticket.

Multi-tenant isolation
---------------------

Every route threads ``tenant_id`` from the JWT, never from path /
query. Cross-tenant lookups are short-circuited by
:meth:`TicketRepository.get_by_id` returning ``None`` (anti-enumeration
parity with the other repos in the codebase). The API layer
translates ``None`` to 404.

PII discipline
--------------

Log lines carry opaque IDs only (``ticket_id``, ``tenant_id``).
NEVER subject text, never payload contents. The subject itself is
PII (first 120 chars of customer message) but the API contract
returns it to authenticated callers — that's the point of a ticket
detail page.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user
from conversation.repository import ConversationRepository
from core.database import get_session
from ticket.repository import TicketRepository
from ticket.schemas import TicketEventOut, TicketOut, TicketTransitionIn
from ticket.service import TicketNotFound, TicketService
from ticket.state_machine import InvalidTransition

router = APIRouter(prefix="/api/v1/tickets", tags=["tickets"])


# ---------------------------------------------------------------------------
# Service factory
# ---------------------------------------------------------------------------


def get_ticket_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
) -> TicketService:
    """Per-request ``TicketService`` with the CANCELLED-cleanup hook wired.

    Wires ``ConversationRepository()`` into the service so that
    transitioning to ``CANCELLED`` NULLs ``conversations.ticket_id``
    atomically with the ticket status update (otherwise deleting the
    ticket later would trip the RESTRICT FK declared on the
    back-pointer — see Task 4 design notes).

    FastAPI's DI cache reuses the same ``session`` for both deps in
    the same request, so the status update + ticket_id NULL write
    commit together (or roll back together) — the session-threading
    fix from Task 5.
    """
    # Bind the tenant contextvar so any downstream repository call
    # (e.g. ``ConversationRepository.clear_ticket_id``) sees the
    # caller's tenant. ``get_current_user`` already sets this; we
    # don't re-set to avoid a duplicate token.
    del claims  # tenant_id is read from the JWT contextvar set by get_current_user
    return TicketService(
        repo=TicketRepository(session),
        conv_repo=ConversationRepository(),
        sla_policy_default_minutes=60,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/{ticket_id}", response_model=TicketOut)
async def get_ticket(
    ticket_id: str,
    svc: Annotated[TicketService, Depends(get_ticket_service)],
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
) -> TicketOut:
    """Fetch a single ticket. 404 on missing or cross-tenant.

    Uses :meth:`TicketRepository.get_by_id` which already scopes by
    ``tenant_id`` — no inline tenant check needed (plan-deviation
    refactor: the original plan duplicated the tenant check inline
    three times).
    """
    tenant_id = claims["tenant_id"]
    ticket = await svc.repo.get_by_id(ticket_id, tenant_id=tenant_id)
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="ticket not found"
        )
    return TicketOut.model_validate(ticket)


@router.post("/{ticket_id}/transition", response_model=TicketOut)
async def transition_ticket(
    ticket_id: str,
    body: TicketTransitionIn,
    svc: Annotated[TicketService, Depends(get_ticket_service)],
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
) -> TicketOut:
    """Drive the state machine for a ticket.

    Status codes:

    * ``200`` — transition applied; returns the updated ticket.
    * ``404`` — ticket missing or cross-tenant (anti-enumeration).
    * ``409 Conflict`` — the transition is illegal per the state
      machine (e.g. ``NEW -> CLOSED`` skipping TRIAGED). NOT 400:
      400 is for malformed requests, 409 is for state conflicts
      (Task 5 reviewer flagged this — see plan-deviation notes).
    """
    tenant_id = claims["tenant_id"]
    actor_id: str | None = claims.get("sub")

    # Load first so 404 short-circuits BEFORE the state machine guard.
    # Otherwise a state-machine error on a missing ticket would leak
    # the ticket's existence via the response code (409 vs 404).
    current = await svc.repo.get_by_id(ticket_id, tenant_id=tenant_id)
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="ticket not found"
        )

    try:
        updated = await svc.transition(
            ticket_id,
            current.status,
            body.to_status,
            actor_type=body.actor_type,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )
    except InvalidTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"illegal transition: {exc}",
        ) from exc
    except TicketNotFound as exc:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="ticket not found"
        ) from exc

    return TicketOut.model_validate(updated)


@router.get("/{ticket_id}/events", response_model=list[TicketEventOut])
async def list_ticket_events(
    ticket_id: str,
    svc: Annotated[TicketService, Depends(get_ticket_service)],
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
) -> list[TicketEventOut]:
    """List audit-log rows for a ticket, newest-first.

    Uses :meth:`TicketRepository.list_events` (added in Task 6) which
    is tenant-scoped at the query site rather than via an inline
    ``session.execute`` in the route — promotes testability and
    keeps the route thin (plan-deviation refactor).

    Returns 404 when the ticket is missing or cross-tenant (so a
    probing caller can't differentiate "no events" from "wrong
    tenant"). An empty event list is a normal ``200 []``.
    """
    tenant_id = claims["tenant_id"]
    # Confirm the ticket is visible to this tenant first; the repo's
    # list_events also confirms, but doing it here gives us the 404
    # surface without an empty-list ambiguity for callers.
    exists = await svc.repo.get_by_id(ticket_id, tenant_id=tenant_id)
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="ticket not found"
        )
    events = await svc.repo.list_events(ticket_id, tenant_id=tenant_id)
    return [TicketEventOut.model_validate(e) for e in events]


__all__ = ["router", "get_ticket_service"]