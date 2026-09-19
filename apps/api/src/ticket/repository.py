"""Repository layer for ``tickets`` / ``ticket_events`` / ``sla_policies``.

The repository takes an injected :class:`AsyncSession` so the API
layer (Task 6) can wire one session per request and let FastAPI's
``get_db`` dependency commit at the end. The tests inject a
``MagicMock`` session — methods only call ``.flush()`` and ``.add()``,
which the mock absorbs.

Tenant isolation
----------------

Every method that takes an opaque ID (``ticket_id``) also requires
``tenant_id`` as a keyword argument. Cross-tenant access returns
``None`` (NOT a 403) so the service layer can translate to
:class:`TicketNotFound` uniformly — anti-enumeration parity with the
other repos in the codebase.

PII discipline
--------------

Logs and exception messages carry only opaque IDs (ULIDs), enum
values, and class names. NEVER ``subject`` / ``category`` — those
can carry tenant-meaningful identifiers.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.id_gen import new_id
from ticket.enums import TicketPriority, TicketStatus
from ticket.models import SlaPolicy, Ticket, TicketEvent


class TicketRepository:
    """CRUD for ``tickets`` / ``ticket_events`` / ``sla_policies``.

    The injected ``session`` is owned by the caller; this class only
    flushes. The caller is responsible for ``commit()`` /
    ``rollback()`` — typically the FastAPI ``get_db`` dependency.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- tickets ----

    async def create(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        subject: str,
        category: str | None = None,
        priority: TicketPriority = TicketPriority.P2,
        sla_deadline_at: datetime | None = None,
    ) -> Ticket:
        """Insert a new Ticket row in NEW state. Caller commits."""
        ticket = Ticket(
            id=new_id(),
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            subject=subject,
            category=category,
            priority=priority,
            status=TicketStatus.NEW,
            sla_deadline_at=sla_deadline_at,
        )
        self.session.add(ticket)
        await self.session.flush()
        return ticket

    async def get_by_id(
        self, ticket_id: str, *, tenant_id: str
    ) -> Ticket | None:
        """Look up a ticket scoped to ``tenant_id``. Cross-tenant returns ``None``.

        Anti-enumeration: a probe for a ticket belonging to another
        tenant yields the same ``None`` as a missing row. The
        service layer translates ``None`` to
        :class:`TicketNotFound`.
        """
        ticket = await self.session.get(Ticket, ticket_id)
        if ticket is not None and ticket.tenant_id != tenant_id:
            return None
        return ticket

    async def get_for_conversation(
        self, conversation_id: str, *, tenant_id: str
    ) -> Ticket | None:
        """Return the ticket attached to ``conversation_id`` for ``tenant_id``.

        A conversation's ticket is unique (UNIQUE on
        ``tickets.conversation_id``), so the WHERE clause yields at
        most one row. ``tenant_id`` is in the clause as
        defence-in-depth: even if a cross-tenant conversation_id
        leaked into the call, the repo wouldn't surface the row.
        """
        stmt = select(Ticket).where(
            Ticket.conversation_id == conversation_id,
            Ticket.tenant_id == tenant_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_status(
        self,
        ticket: Ticket,
        status: TicketStatus,
        *,
        tenant_id: str,
        resolved_at: datetime | None = None,
        closed_at: datetime | None = None,
    ) -> Ticket:
        """Update ``ticket.status`` (and optional timestamps) on the loaded row.

        Defensive tenant check: refuses to mutate a ticket that
        doesn't belong to ``tenant_id``. This catches a logic bug in
        the caller (e.g. forgetting to thread ``tenant_id`` through)
        rather than silently leaking the write.
        """
        if ticket.tenant_id != tenant_id:
            raise PermissionError(
                f"ticket {ticket.id} does not belong to tenant {tenant_id}"
            )
        ticket.status = status
        if resolved_at is not None:
            ticket.resolved_at = resolved_at
        if closed_at is not None:
            ticket.closed_at = closed_at
        await self.session.flush()
        return ticket

    async def delete_by_id(
        self, ticket_id: str, *, tenant_id: str
    ) -> bool:
        """Delete a ticket scoped to ``tenant_id``. Returns True on delete.

        Returns ``False`` when the ticket doesn't exist or belongs to
        a different tenant (same shape as ``get_by_id``). The
        ``events`` relationship cascades via ``all, delete-orphan``,
        so audit rows go with the ticket.

        Reserved for future cleanup flows — Task 5 does not wire a
        caller. Kept here so the test-suite has a single home for
        tenant-scoped deletion semantics.
        """
        ticket = await self.get_by_id(ticket_id, tenant_id=tenant_id)
        if ticket is None:
            return False
        await self.session.delete(ticket)
        await self.session.flush()
        return True

    # ---- ticket_events ----

    async def append_event(
        self,
        *,
        ticket_id: str,
        tenant_id: str,
        actor_type: str,
        actor_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> TicketEvent:
        """Insert one audit-log row. Caller commits."""
        event = TicketEvent(
            id=new_id(),
            ticket_id=ticket_id,
            tenant_id=tenant_id,
            actor_type=actor_type,
            actor_id=actor_id,
            event_type=event_type,
            payload=payload,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    # ---- sla_policies ----

    async def find_sla_policy(
        self, tenant_id: str, priority: TicketPriority
    ) -> SlaPolicy | None:
        """Return the (single) SLA policy for ``(tenant_id, priority)``.

        Tenants can have multiple policies per priority bucket
        (labelled by ``name``); for M1 we pick the most-recently-
        created one when multiple exist. If none exists, the service
        falls back to a hard-coded default — see
        :class:`TicketService.create`.
        """
        stmt = (
            select(SlaPolicy)
            .where(
                SlaPolicy.tenant_id == tenant_id,
                SlaPolicy.priority == priority,
            )
            .order_by(SlaPolicy.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()


__all__ = ["TicketRepository"]