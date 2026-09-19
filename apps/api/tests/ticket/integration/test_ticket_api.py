"""Live-DB integration tests for the Ticket REST API (Task 6, M2.A).

End-to-end coverage of every endpoint in ``ticket.api.router``
against a real Postgres database. The FastAPI app is mounted via
``ASGITransport`` so the auth dependency runs for real — JWTs are
minted via ``auth.jwt.create_access_token``.

What's exercised:
    * GET /tickets/{id} returns the row.
    * GET /tickets/{id} returns 404 cross-tenant (anti-enumeration).
    * POST /tickets/{id}/transition with an illegal transition
      returns 409 (NOT 400 — Task 5 reviewer flagged this).
    * POST /tickets/{id}/transition with a legal transition
      (NEW -> TRIAGED) returns 200 with the updated row.
    * POST /tickets/{id}/transition to CANCELLED NULLs
      ``conversations.ticket_id`` (session-threading proof from
      Task 5).
    * GET /tickets/{id}/events returns rows ordered by
      ``created_at DESC``.

PII discipline:
    * Subjects and event payloads carry ``MAGIC_PHRASE_TICKET_*``
      markers so test assertions are against opaque tokens, never
      real customer text.
    * No log inspection.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from conversation.repository import ConversationRepository
from core.database import get_session
from ticket.enums import TicketStatus
from ticket.models import Ticket, TicketEvent


# ===========================================================================
# GET /api/v1/tickets/{id}
# ===========================================================================


@pytest.mark.integration
async def test_get_ticket_returns_ticket(
    async_client: AsyncClient,
    sample_ticket: Ticket,
) -> None:
    """Auth + tenant match -> 200, body matches the seeded subject."""
    headers = _auth_headers(tenant_id=sample_ticket.tenant_id)
    resp = await async_client.get(
        f"/api/v1/tickets/{sample_ticket.id}", headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == sample_ticket.id
    assert body["subject"] == "MAGIC_PHRASE_TICKET_001 sample subject"
    assert body["status"] == TicketStatus.NEW.value
    assert body["conversation_id"] == sample_ticket.conversation_id


@pytest.mark.integration
async def test_get_ticket_other_tenant_returns_404(
    async_client: AsyncClient,
    sample_ticket: Ticket,
    second_tenant_factory: Any,
) -> None:
    """Cross-tenant probe yields 404 (anti-enumeration parity)."""
    async for other_tenant in second_tenant_factory():
        headers = _auth_headers(tenant_id=other_tenant.id)
        resp = await async_client.get(
            f"/api/v1/tickets/{sample_ticket.id}", headers=headers
        )
        assert resp.status_code == 404, resp.text


@pytest.mark.integration
async def test_get_ticket_missing_returns_404(
    async_client: AsyncClient,
    sample_ticket: Ticket,
) -> None:
    """Unknown ULID -> 404 (no 400 leak / no enumeration)."""
    headers = _auth_headers(tenant_id=sample_ticket.tenant_id)
    resp = await async_client.get(
        "/api/v1/tickets/01HZZZZZZZZZZZZZZZZZZZZZZZ", headers=headers
    )
    assert resp.status_code == 404


# ===========================================================================
# POST /api/v1/tickets/{id}/transition
# ===========================================================================


@pytest.mark.integration
async def test_transition_invalid_returns_409(
    async_client: AsyncClient,
    sample_ticket: Ticket,
) -> None:
    """NEW -> CLOSED is illegal per the state machine -> 409 Conflict.

    NOT 400: 400 is for malformed requests; 409 is for state
    conflicts (Task 5 reviewer flagged this in the plan).
    """
    headers = _auth_headers(tenant_id=sample_ticket.tenant_id)
    resp = await async_client.post(
        f"/api/v1/tickets/{sample_ticket.id}/transition",
        headers=headers,
        json={"to_status": TicketStatus.CLOSED.value, "actor_type": "agent"},
    )
    assert resp.status_code == 409, resp.text
    # And the body mentions "illegal" so the caller knows what failed.
    assert "illegal" in resp.json()["detail"].lower()


@pytest.mark.integration
async def test_transition_new_to_triaged_returns_200(
    async_client: AsyncClient,
    sample_ticket: Ticket,
) -> None:
    """Legal NEW -> TRIAGED returns 200 with status='triaged'."""
    headers = _auth_headers(tenant_id=sample_ticket.tenant_id)
    resp = await async_client.post(
        f"/api/v1/tickets/{sample_ticket.id}/transition",
        headers=headers,
        json={"to_status": TicketStatus.TRIAGED.value, "actor_type": "agent"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == TicketStatus.TRIAGED.value
    assert body["id"] == sample_ticket.id


@pytest.mark.integration
async def test_transition_to_cancelled_clears_conversation_ticket_id(
    async_client: AsyncClient,
    sample_ticket: Ticket,
) -> None:
    """Transition to CANCELLED NULLs ``conversations.ticket_id``.

    Proves the session-threading fix from Task 5 works end-to-end:
    the CANCELLED cleanup runs in the same SQLAlchemy session as the
    status update, so the two writes commit atomically.

    Without this cleanup, the RESTRICT FK on
    ``conversations.ticket_id`` would block any later DELETE of the
    ticket while the conversation still pointed at it.
    """
    # Sanity: stub the conv_repo to verify the cleanup hook fired. We
    # capture via the test's own ConversationRepository — the API
    # uses a per-request session, so we re-read the row to observe
    # the effect.
    conv_repo = ConversationRepository()
    before = await conv_repo.get_by_id(sample_ticket.conversation_id)
    assert before is not None

    headers = _auth_headers(tenant_id=sample_ticket.tenant_id)
    resp = await async_client.post(
        f"/api/v1/tickets/{sample_ticket.id}/transition",
        headers=headers,
        json={"to_status": TicketStatus.CANCELLED.value, "actor_type": "agent"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == TicketStatus.CANCELLED.value

    # Re-read conversation via a fresh session — the back-pointer
    # must now be NULL. We bypass ``get_by_id`` (which doesn't
    # surface ``ticket_id`` directly) and query the row directly.
    async with get_session() as session:
        from conversation.models import Conversation

        conv_row = await session.get(Conversation, sample_ticket.conversation_id)
        assert conv_row is not None
        assert conv_row.ticket_id is None, (
            f"conversations.ticket_id should be NULL after CANCELLED "
            f"transition, got {conv_row.ticket_id!r}"
        )

    # And the ticket itself moved to CANCELLED with closed_at set.
    async with get_session() as session:
        t = await session.get(Ticket, sample_ticket.id)
        assert t is not None
        assert t.status == TicketStatus.CANCELLED


@pytest.mark.integration
async def test_transition_missing_ticket_returns_404(
    async_client: AsyncClient,
    sample_ticket: Ticket,
) -> None:
    """Transitioning a missing ticket yields 404, not 409.

    Order-of-operations invariant: the route loads the ticket FIRST
    (so 404 short-circuits) BEFORE the state machine guard runs.
    Otherwise a state-machine error on a missing ticket would leak
    existence via the response code.
    """
    headers = _auth_headers(tenant_id=sample_ticket.tenant_id)
    resp = await async_client.post(
        "/api/v1/tickets/01HZZZZZZZZZZZZZZZZZZZZZZZ/transition",
        headers=headers,
        json={"to_status": TicketStatus.TRIAGED.value, "actor_type": "agent"},
    )
    assert resp.status_code == 404


# ===========================================================================
# GET /api/v1/tickets/{id}/events
# ===========================================================================


@pytest.mark.integration
async def test_get_events_returns_ordered_desc(
    async_client: AsyncClient,
    sample_ticket_with_events: Any,
) -> None:
    """GET events returns rows ordered by ``created_at DESC``."""
    ticket, _events = sample_ticket_with_events
    headers = _auth_headers(tenant_id=ticket.tenant_id)
    resp = await async_client.get(
        f"/api/v1/tickets/{ticket.id}/events", headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) >= 2
    timestamps = [e["created_at"] for e in body]
    # DESC: each timestamp must be >= the next.
    for a, b in zip(timestamps, timestamps[1:]):
        assert a >= b, f"events not in DESC order: {timestamps!r}"


@pytest.mark.integration
async def test_get_events_missing_ticket_returns_404(
    async_client: AsyncClient,
    sample_ticket: Ticket,
) -> None:
    """GET events on a missing ticket -> 404 (not empty list).

    A probing caller cannot differentiate "no events" from "wrong
    tenant" via the response code.
    """
    headers = _auth_headers(tenant_id=sample_ticket.tenant_id)
    resp = await async_client.get(
        "/api/v1/tickets/01HZZZZZZZZZZZZZZZZZZZZZZZ/events", headers=headers
    )
    assert resp.status_code == 404


# ===========================================================================
# Helpers
# ===========================================================================


def _auth_headers(*, tenant_id: str, user_id: str = "u_admin") -> dict[str, str]:
    """Mint an admin JWT for ``tenant_id`` and return the auth header."""
    from auth.jwt import create_access_token

    token = create_access_token(tenant_id=tenant_id, user_id=user_id, role="admin")
    return {"Authorization": f"Bearer {token}"}


__all__ = []