"""Smoke test: model imports + enum values + simple instantiation in-memory."""
from ticket.enums import TicketPriority, TicketStatus
from ticket.models import SlaPolicy, Ticket, TicketEvent


def test_ticket_priority_values() -> None:
    assert TicketPriority.P0 == "P0"
    assert TicketPriority.P3 == "P3"
    assert len(list(TicketPriority)) == 4


def test_ticket_status_values() -> None:
    assert TicketStatus.NEW == "new"
    assert TicketStatus.CLOSED == "closed"
    assert len(list(TicketStatus)) == 7


def test_ticket_event_columns() -> None:
    """TicketEvent carries the audit-log shape: tenant + ticket + actor + event + payload."""
    cols = {c.name for c in TicketEvent.__table__.columns}
    assert "ticket_id" in cols
    assert "actor_type" in cols
    assert "event_type" in cols
    assert "payload" in cols


def test_ticket_indexes() -> None:
    """Verify the three indexes are declared correctly."""
    indexes = {idx.name for idx in Ticket.__table__.indexes}
    assert "idx_tickets_tenant_status" in indexes
    assert "idx_tickets_assignee" in indexes
    assert "idx_tickets_sla" in indexes


def test_sla_policy_unique_constraint() -> None:
    """SlaPolicy is unique per (tenant_id, name)."""
    constraints = {c.name for c in SlaPolicy.__table__.constraints}
    assert "uq_sla_policies_tenant_name" in constraints


def test_ticket_conversation_unique() -> None:
    """A ticket is 1:1 with a Conversation — enforced by UNIQUE on conversation_id."""
    constraint_names = {c.name for c in Ticket.__table__.constraints}
    assert "uq_tickets_conversation_id" in constraint_names


def test_conversation_has_ticket_id() -> None:
    """Conversation.ticket_id is the back-pointer to the ticket (if any)."""
    from conversation.models import Conversation

    cols = {c.name for c in Conversation.__table__.columns}
    assert "ticket_id" in cols