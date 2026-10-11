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

from fastapi import APIRouter, Depends, HTTPException, Response, status

from auth.dependencies import get_current_user
from conversation.repository import ConversationRepository
from core.database import get_sessionmaker
from ticket.repository import TicketRepository
from ticket.schemas import TicketEventOut, TicketNoteCreate, TicketOut, TicketTransitionIn
from ticket.service import TicketNotFound, TicketService
from ticket.state_machine import InvalidTransition

router = APIRouter(prefix="/api/v1/tickets", tags=["tickets"])


# ---------------------------------------------------------------------------
# Service factory
# ---------------------------------------------------------------------------


async def get_ticket_service(
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
) -> Any:
    """Per-request ``TicketService`` with the CANCELLED-cleanup hook wired.

    Generator dep: opens a fresh ``AsyncSession`` for the request, hands
    the wired ``TicketService`` to the route, and runs the
    ``async with`` cleanup on the way out (commit on clean exit,
    rollback on exception — same semantics as ``core.database.get_session``
    used directly).

    The earlier pattern (``Depends(get_session)``) was a misuse —
    ``get_session`` is an ``@asynccontextmanager``, not a session
    factory, so FastAPI's DI was receiving the context-manager object
    and failing to ``__aiter__`` it (``_AsyncGeneratorContextManager
    object is not an async iterator``). Switching to a generator dep
    that calls ``get_sessionmaker()()`` is the standard FastAPI recipe
    for "yield a session for one request".
    """
    # tenant_id is read from the JWT contextvar set by get_current_user;
    # the parameter is only here to enforce auth ordering.
    del claims
    sm = get_sessionmaker()
    async with sm() as session:
        yield TicketService(
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


# ---------------------------------------------------------------------------
# Tier 2 Task 2.6 — Internal notes.
#
# POST   /tickets/{ticket_id}/notes — append an internal note (agent-only
#                                     audit row, ``event_type="note"``).
# DELETE /tickets/{ticket_id}/notes/{note_id} — author can delete their
#                                              own note; all other cases
#                                              return 404 (anti-tampering
#                                              + anti-enumeration).
#
# Notes ride the same ``TicketEvent`` audit table as state changes,
# so the existing GET /events endpoint is the source of truth — the
# SPA filters ``event_type === 'note'`` to render the dedicated panel.
# No new migration needed.
# ---------------------------------------------------------------------------


@router.post(
    "/{ticket_id}/notes",
    response_model=TicketEventOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_ticket_note(
    ticket_id: str,
    payload: TicketNoteCreate,
    svc: Annotated[TicketService, Depends(get_ticket_service)],
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
) -> TicketEventOut:
    """Append an internal note to ``ticket_id``.

    Body is plain text (1-4096 chars). Author is taken from the JWT
    ``sub`` claim — agents cannot post notes on behalf of other
    agents. Notes are NOT visible to end-customers; they live in
    the agent workspace and admin SPA only.

    Returns 404 for missing / cross-tenant tickets (anti-enumeration).
    """
    tenant_id = claims["tenant_id"]
    author_id = claims.get("sub", "")
    try:
        event = await svc.add_note(
            ticket_id,
            tenant_id=tenant_id,
            author_id=author_id,
            body=payload.body,
        )
    except TicketNotFound as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="ticket not found"
        ) from e
    return TicketEventOut.model_validate(event)


@router.delete(
    "/{ticket_id}/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_ticket_note(
    ticket_id: str,
    note_id: str,
    svc: Annotated[TicketService, Depends(get_ticket_service)],
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
) -> Response:
    """Delete a note the caller authored.

    Only the original author can delete their own note. All
    failure modes (missing / cross-tenant / cross-ticket / not a
    note / wrong author) collapse to 404 — anti-enumeration
    parity with the rest of the API surface, and anti-tampering
    (cannot enumerate whether a sibling agent has notes on the
    ticket).
    """
    tenant_id = claims["tenant_id"]
    author_id = claims.get("sub", "")
    ok = await svc.delete_note(
        ticket_id,
        note_id,
        tenant_id=tenant_id,
        author_id=author_id,
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="note not found"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["get_ticket_service", "router"]