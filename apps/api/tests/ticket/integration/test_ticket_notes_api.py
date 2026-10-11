"""Integration tests for the ticket internal-notes endpoints.

Tier 2 Task 2.6 — internal notes on a ticket.

Two endpoints:

* ``POST   /tickets/{ticket_id}/notes`` — append an agent note.
  The note rides the existing :class:`TicketEvent` audit table
  with ``event_type="note"`` — no new migration needed.
* ``DELETE /tickets/{ticket_id}/notes/{note_id}`` — only the
  original author may delete their own note. All other cases
  collapse to 404 (anti-enumeration + anti-tampering).

Multi-tenant isolation
----------------------
* Cross-tenant access returns 404.
* Notes authored by another agent cannot be deleted by a sibling.

These tests use the ticket-only FastAPI app from
``tests/ticket/integration/conftest.py`` so they don't depend on
the full app's middleware stack.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from ticket.models import Ticket, TicketEvent


def _hdr(*, tenant_id: str, user_id: str = "u_admin", role: str = "admin") -> dict[str, str]:
    # _auth_headers hardcodes role="admin" + user_id="u_admin"; we
    # need flexible identity for the author-can-only-delete-own-note
    # test, so re-mint here.
    from auth.jwt import create_access_token

    token = create_access_token(
        tenant_id=tenant_id, user_id=user_id, role=role
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# POST /tickets/{ticket_id}/notes
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_note_creates_event_with_note_event_type(
    async_client: AsyncClient, sample_ticket: Ticket
) -> None:
    """POST creates an audit event with event_type='note' + body payload."""
    resp = await async_client.post(
        f"/api/v1/tickets/{sample_ticket.id}/notes",
        json={"body": "customer asked for a refund"},
        headers=_hdr(tenant_id=sample_ticket.tenant_id, user_id="u_alice"),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["event_type"] == "note"
    assert body["actor_type"] == "agent"
    assert body["actor_id"] == "u_alice"
    assert body["payload"]["body"] == "customer asked for a refund"
    assert "id" in body and "created_at" in body


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_note_appears_in_events_endpoint(
    async_client: AsyncClient, sample_ticket: Ticket
) -> None:
    """The note shows up in GET /events alongside any system events."""
    create = await async_client.post(
        f"/api/v1/tickets/{sample_ticket.id}/notes",
        json={"body": "follow-up"},
        headers=_hdr(tenant_id=sample_ticket.tenant_id),
    )
    assert create.status_code == 201, create.text

    listing = await async_client.get(
        f"/api/v1/tickets/{sample_ticket.id}/events",
        headers=_hdr(tenant_id=sample_ticket.tenant_id),
    )
    assert listing.status_code == 200, listing.text
    rows = listing.json()
    # Newest first; the note we just added should be at index 0.
    assert rows[0]["event_type"] == "note"
    assert rows[0]["payload"]["body"] == "follow-up"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_note_rejects_empty_body(
    async_client: AsyncClient, sample_ticket: Ticket
) -> None:
    """Empty body fails Pydantic min_length=1 → 422."""
    resp = await async_client.post(
        f"/api/v1/tickets/{sample_ticket.id}/notes",
        json={"body": ""},
        headers=_hdr(tenant_id=sample_ticket.tenant_id),
    )
    assert resp.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_note_rejects_oversize_body(
    async_client: AsyncClient, sample_ticket: Ticket
) -> None:
    """Body > 4096 chars fails Pydantic max_length=4096 → 422."""
    resp = await async_client.post(
        f"/api/v1/tickets/{sample_ticket.id}/notes",
        json={"body": "x" * 4097},
        headers=_hdr(tenant_id=sample_ticket.tenant_id),
    )
    assert resp.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_note_cross_tenant_returns_404(
    async_client: AsyncClient,
    sample_ticket: Ticket,
    second_tenant_factory,
) -> None:
    """Cross-tenant POST is 404 (anti-enumeration)."""
    async for other in second_tenant_factory():
        resp = await async_client.post(
            f"/api/v1/tickets/{sample_ticket.id}/notes",
            json={"body": "should fail"},
            headers=_hdr(tenant_id=other.id),
        )
        assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_note_unknown_ticket_returns_404(
    async_client: AsyncClient, sample_ticket: Ticket
) -> None:
    """Unknown ticket_id returns 404 (not 422)."""
    fake_id = "01HZFAKE00000000000000000"
    resp = await async_client.post(
        f"/api/v1/tickets/{fake_id}/notes",
        json={"body": "hi"},
        headers=_hdr(tenant_id=sample_ticket.tenant_id),
    )
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_note_requires_auth(
    async_client: AsyncClient, sample_ticket: Ticket
) -> None:
    """Missing bearer token → 401."""
    resp = await async_client.post(
        f"/api/v1/tickets/{sample_ticket.id}/notes",
        json={"body": "hi"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# DELETE /tickets/{ticket_id}/notes/{note_id}
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_author_can_delete_own_note(
    async_client: AsyncClient, sample_ticket: Ticket
) -> None:
    """Author deleting their own note succeeds (204) and the row is gone."""
    create = await async_client.post(
        f"/api/v1/tickets/{sample_ticket.id}/notes",
        json={"body": "delete me"},
        headers=_hdr(tenant_id=sample_ticket.tenant_id, user_id="u_alice"),
    )
    assert create.status_code == 201
    note_id = create.json()["id"]

    delete = await async_client.delete(
        f"/api/v1/tickets/{sample_ticket.id}/notes/{note_id}",
        headers=_hdr(tenant_id=sample_ticket.tenant_id, user_id="u_alice"),
    )
    assert delete.status_code == 204

    # Confirm via /events — the note is gone.
    listing = await async_client.get(
        f"/api/v1/tickets/{sample_ticket.id}/events",
        headers=_hdr(tenant_id=sample_ticket.tenant_id),
    )
    rows = listing.json()
    assert all(r["id"] != note_id for r in rows)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_other_agent_cannot_delete_sibling_note(
    async_client: AsyncClient, sample_ticket: Ticket
) -> None:
    """Anti-tampering: Bob cannot delete Alice's note → 404."""
    create = await async_client.post(
        f"/api/v1/tickets/{sample_ticket.id}/notes",
        json={"body": "alice only"},
        headers=_hdr(tenant_id=sample_ticket.tenant_id, user_id="u_alice"),
    )
    assert create.status_code == 201
    note_id = create.json()["id"]

    delete = await async_client.delete(
        f"/api/v1/tickets/{sample_ticket.id}/notes/{note_id}",
        headers=_hdr(tenant_id=sample_ticket.tenant_id, user_id="u_bob"),
    )
    assert delete.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_cannot_delete_other_agents_note(
    async_client: AsyncClient, sample_ticket: Ticket
) -> None:
    """Even an admin/owner cannot bypass author-only — 404 (anti-tampering)."""
    create = await async_client.post(
        f"/api/v1/tickets/{sample_ticket.id}/notes",
        json={"body": "alice wrote this"},
        headers=_hdr(
            tenant_id=sample_ticket.tenant_id, user_id="u_alice", role="admin"
        ),
    )
    assert create.status_code == 201
    note_id = create.json()["id"]

    delete = await async_client.delete(
        f"/api/v1/tickets/{sample_ticket.id}/notes/{note_id}",
        headers=_hdr(
            tenant_id=sample_ticket.tenant_id, user_id="u_owner", role="owner"
        ),
    )
    assert delete.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cannot_delete_state_change_event_through_note_path(
    async_client: AsyncClient, sample_ticket_with_events: tuple[Ticket, list[TicketEvent]]
) -> None:
    """A status_changed event cannot be removed via the notes endpoint."""
    ticket, events = sample_ticket_with_events
    status_event = next(e for e in events if e.event_type == "status_changed")

    resp = await async_client.delete(
        f"/api/v1/tickets/{ticket.id}/notes/{status_event.id}",
        headers=_hdr(tenant_id=ticket.tenant_id, user_id="u_agent_1"),
    )
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cannot_delete_note_on_other_ticket(
    async_client: AsyncClient,
    sample_ticket: Ticket,
    sample_ticket_with_events: tuple[Ticket, list[TicketEvent]],
) -> None:
    """Note attached to ticket-A cannot be deleted via ticket-B path → 404."""
    # Create a note on sample_ticket (Ticket A).
    create = await async_client.post(
        f"/api/v1/tickets/{sample_ticket.id}/notes",
        json={"body": "ticket A note"},
        headers=_hdr(tenant_id=sample_ticket.tenant_id, user_id="u_alice"),
    )
    note_id = create.json()["id"]

    other_ticket, _ = sample_ticket_with_events
    # Try to delete via the other ticket's path.
    resp = await async_client.delete(
        f"/api/v1/tickets/{other_ticket.id}/notes/{note_id}",
        headers=_hdr(tenant_id=sample_ticket.tenant_id, user_id="u_alice"),
    )
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_note_cross_tenant_returns_404(
    async_client: AsyncClient,
    sample_ticket: Ticket,
    second_tenant_factory,
) -> None:
    """Cross-tenant DELETE is 404 (anti-enumeration)."""
    create = await async_client.post(
        f"/api/v1/tickets/{sample_ticket.id}/notes",
        json={"body": "mine"},
        headers=_hdr(tenant_id=sample_ticket.tenant_id, user_id="u_alice"),
    )
    note_id = create.json()["id"]

    async for other in second_tenant_factory():
        resp = await async_client.delete(
            f"/api/v1/tickets/{sample_ticket.id}/notes/{note_id}",
            headers=_hdr(tenant_id=other.id, user_id="u_alice"),
        )
        assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_unknown_note_returns_404(
    async_client: AsyncClient, sample_ticket: Ticket
) -> None:
    """Unknown note id → 404 (no enumeration)."""
    resp = await async_client.delete(
        f"/api/v1/tickets/{sample_ticket.id}/notes/01HZFAKE00000000000000000",
        headers=_hdr(tenant_id=sample_ticket.tenant_id),
    )
    assert resp.status_code == 404


__all__ = [
    "test_admin_cannot_delete_other_agents_note",
    "test_author_can_delete_own_note",
    "test_cannot_delete_note_on_other_ticket",
    "test_cannot_delete_state_change_event_through_note_path",
    "test_delete_note_cross_tenant_returns_404",
    "test_delete_unknown_note_returns_404",
    "test_other_agent_cannot_delete_sibling_note",
    "test_post_note_appears_in_events_endpoint",
    "test_post_note_creates_event_with_note_event_type",
    "test_post_note_cross_tenant_returns_404",
    "test_post_note_rejects_empty_body",
    "test_post_note_rejects_oversize_body",
    "test_post_note_requires_auth",
    "test_post_note_unknown_ticket_returns_404",
]