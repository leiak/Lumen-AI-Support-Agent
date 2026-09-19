"""Service-layer tests for the Ticket domain.

All tests use mocked repositories (no real DB). The ``mock_repo``
fixture is shaped so that ``get_by_id`` returns a row whose
``tenant_id`` matches the caller (``"t1"``), so the tenant-scoping
logic in :class:`TicketService` exercises the happy path by default.
Tests that need a cross-tenant probe override ``get_by_id`` to return
``None`` (anti-enumeration parity with the repo's own cross-tenant
behavior).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from ticket.enums import TicketPriority, TicketStatus
from ticket.service import TicketNotFound, TicketService
from ticket.state_machine import InvalidTransition


@pytest.fixture
def mock_repo():
    """Repo mock shaped to mirror the real ``TicketRepository`` contract.

    ``get_by_id`` and ``get_for_conversation`` accept ``tenant_id`` as
    a kwarg and return a ticket whose ``tenant_id`` matches (or None
    for the conversation helper, which is the "no existing ticket"
    case used by ``get_or_create_for_conversation``).
    """
    repo = MagicMock()
    repo.create = AsyncMock(
        side_effect=lambda **kw: MagicMock(
            id="t-new",
            tenant_id=kw.get("tenant_id"),
            conversation_id=kw.get("conversation_id"),
            status=TicketStatus.NEW,
            priority=kw.get("priority", TicketPriority.P2),
            subject=kw.get("subject"),
            category=kw.get("category"),
            sla_deadline_at=kw.get("sla_deadline_at"),
        )
    )
    repo.get_by_id = AsyncMock(
        return_value=MagicMock(
            id="t1",
            tenant_id="t1",
            conversation_id="c-existing",
            status=TicketStatus.NEW,
            priority=TicketPriority.P2,
        )
    )
    repo.get_for_conversation = AsyncMock(return_value=None)
    repo.update_status = AsyncMock(
        side_effect=lambda ticket, status, **kw: setattr(ticket, "status", status) or ticket
    )
    repo.append_event = AsyncMock()
    repo.find_sla_policy = AsyncMock(
        return_value=MagicMock(first_response_minutes=60)
    )
    return repo


@pytest.mark.asyncio
async def test_create_default_priority_p2(mock_repo):
    svc = TicketService(repo=mock_repo, sla_policy_default_minutes=60)
    ticket = await svc.create(
        tenant_id="t1", conversation_id="c1", subject="test"
    )
    assert ticket.priority == TicketPriority.P2
    assert ticket.status == TicketStatus.NEW
    mock_repo.append_event.assert_awaited()


@pytest.mark.asyncio
async def test_create_applies_sla_policy(mock_repo):
    svc = TicketService(repo=mock_repo, sla_policy_default_minutes=60)
    ticket = await svc.create(
        tenant_id="t1",
        conversation_id="c1",
        subject="test",
        priority=TicketPriority.P0,
    )
    assert ticket.sla_deadline_at is not None


@pytest.mark.asyncio
async def test_transition_new_to_triaged(mock_repo):
    svc = TicketService(repo=mock_repo, sla_policy_default_minutes=60)
    updated = await svc.transition(
        "t1",
        TicketStatus.NEW,
        TicketStatus.TRIAGED,
        actor_type="agent",
        actor_id="a1",
        tenant_id="t1",
    )
    assert updated.status == TicketStatus.TRIAGED


@pytest.mark.asyncio
async def test_transition_invalid_raises(mock_repo):
    svc = TicketService(repo=mock_repo, sla_policy_default_minutes=60)
    with pytest.raises(InvalidTransition):
        await svc.transition(
            "t1",
            TicketStatus.NEW,
            TicketStatus.CLOSED,
            actor_type="agent",
            actor_id="a1",
            tenant_id="t1",
        )


@pytest.mark.asyncio
async def test_get_or_create_for_conversation_returns_existing(mock_repo):
    mock_repo.get_for_conversation = AsyncMock(
        return_value=MagicMock(id="existing")
    )
    svc = TicketService(repo=mock_repo, sla_policy_default_minutes=60)
    ticket = await svc.get_or_create_for_conversation(
        tenant_id="t1", conversation_id="c1", subject="x"
    )
    assert ticket.id == "existing"
    mock_repo.create.assert_not_awaited()


# ---------------------------------------------------------------------------
# Plan-deviation tests: tenant scoping + CANCELLED-side cleanup of the
# conversations.ticket_id back-pointer (the RESTRICT FK from Task 4
# prevents deleting a ticket while the conversation still points at
# it, so we MUST null the back-pointer on CANCELLED).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_to_cancelled_clears_conversation_ticket_id(mock_repo):
    """Transitioning to CANCELLED must NULL conversations.ticket_id.

    Without this cleanup, the RESTRICT FK on ``conversations.ticket_id``
    blocks any future delete of the ticket. The service must
    coordinate the back-pointer NULL via the injected
    ``ConversationRepository``.
    """
    mock_conv_repo = MagicMock()
    mock_conv_repo.clear_ticket_id = AsyncMock()
    svc = TicketService(
        repo=mock_repo,
        conv_repo=mock_conv_repo,
        sla_policy_default_minutes=60,
    )
    await svc.transition(
        "t1",
        TicketStatus.NEW,
        TicketStatus.CANCELLED,
        actor_type="agent",
        actor_id="a1",
        tenant_id="t1",
    )
    mock_conv_repo.clear_ticket_id.assert_awaited_once_with(
        conversation_id="c-existing", tenant_id="t1"
    )


@pytest.mark.asyncio
async def test_transition_cross_tenant_returns_none(mock_repo):
    """get_by_id with wrong tenant_id returns None (no exception, no leak).

    The repo's tenant scoping short-circuits to ``None`` for a
    cross-tenant lookup; the service MUST translate that to
    :class:`TicketNotFound` rather than 403 or a generic exception,
    so the API layer can uniformly map to 404 (anti-enumeration).
    """
    mock_repo.get_by_id = AsyncMock(return_value=None)
    svc = TicketService(repo=mock_repo, sla_policy_default_minutes=60)
    with pytest.raises(TicketNotFound):
        await svc.transition(
            "t1",
            TicketStatus.NEW,
            TicketStatus.TRIAGED,
            actor_type="agent",
            actor_id="a1",
            tenant_id="wrong-tenant",
        )