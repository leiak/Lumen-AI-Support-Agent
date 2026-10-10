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


def test_saenum_columns_use_lowercase_values() -> None:
    """Regression: SAEnum columns must round-trip lowercase PG ENUM values.

    The PG types ``ticket_priority`` and ``ticket_status`` were created
    by the alembic migration with lowercase values (``{new, triaged,
    in_progress, ...}`` and ``{P0, P1, P2, P3}``). Without
    ``values_callable``, SAEnum sends ``Enum.name`` (uppercase) on
    write and fails with ``LookupError: 'in_progress' is not among
    the defined enum values`` on read — every ORM round-trip on
    ``Ticket.priority`` / ``Ticket.status`` / ``SlaPolicy.priority``
    was broken. This test pins the fix: each enum column must
    declare ``values_callable`` so the column treats the enum
    members by their lowercase ``.value``.
    """
    from sqlalchemy import Enum as SAEnum

    # (column, enum_cls, must_contain_literal) — the literal
    # check is the one that actually verifies the fix (proves the
    # column maps to the *PG-side* value, not just any enum member).
    cases = [
        (Ticket.__table__.c.priority, TicketPriority, "P0"),
        (Ticket.__table__.c.status, TicketStatus, "in_progress"),
        (SlaPolicy.__table__.c.priority, TicketPriority, "P2"),
    ]
    for col, enum_cls, expected_literal in cases:
        assert isinstance(col.type, SAEnum), (
            f"{col} must be SAEnum-backed so PG ENUM types are honored"
        )
        assert col.type.values_callable is not None, (
            f"{col} must declare values_callable — without it SAEnum "
            "emits Enum.name (uppercase) and PG rejects it"
        )
        resolved = col.type.values_callable(enum_cls)
        assert resolved == [e.value for e in enum_cls], (
            f"{col}.values_callable should return each enum's .value "
            f"(lowercase); got {resolved!r}"
        )
        assert expected_literal in resolved, (
            f"{col} must resolve to {expected_literal!r} to match the "
            f"PG ENUM literal; resolved={resolved!r}"
        )