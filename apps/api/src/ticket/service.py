"""Business logic for the Ticket domain.

Sits between the API layer (Task 6) and :class:`TicketRepository`.
Responsibilities:

* Tenant isolation enforcement on every read/write. ``TicketRepository``
  already scopes reads by ``tenant_id``; the service translates the
  ``None`` return into :class:`TicketNotFound` so the API layer can
  map uniformly to 404 (anti-enumeration).
* Driving the state machine: every ``transition`` call runs
  :func:`ticket.state_machine.transition` BEFORE any DB write, so an
  illegal transition never reaches the database.
* Writing one :class:`TicketEvent` row per state change.
* Coordinating the cross-table cleanup on CANCELLED — transitioning
  to ``CANCELLED`` also NULLs ``conversations.ticket_id`` (in the
  same SQLAlchemy session as the status update, so the two writes
  commit atomically) so the ticket can later be deleted without
  tripping the RESTRICT FK declared on the back-pointer (see Task 4
  follow-up note #2).

PII discipline
--------------

Logs carry opaque IDs only (``ticket_id``, ``tenant_id``,
``conversation_id``). NEVER ``subject`` / ``category`` / ``payload``
contents.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from core.logging import get_logger
from ticket.enums import TicketPriority, TicketStatus
from ticket.repository import TicketRepository
from ticket.state_machine import transition as sm_transition

if TYPE_CHECKING:
    from conversation.repository import ConversationRepository
    from ticket.models import Ticket


log = get_logger(__name__)


class TicketNotFound(Exception):
    """Raised when a ticket lookup returns ``None`` (missing or cross-tenant).

    The API layer maps this to ``404``. Using a single exception
    class for both "missing" and "cross-tenant" prevents enumeration
    via response differentiation (status code / body).
    """


class TicketService:
    """Tenant-scoped business logic for Tickets."""

    def __init__(
        self,
        repo: TicketRepository,
        conv_repo: "ConversationRepository | None" = None,
        *,
        sla_policy_default_minutes: int = 60,
    ) -> None:
        self.repo = repo
        # ``conv_repo`` is injected so the CANCELLED transition can
        # NULL ``conversations.ticket_id`` without leaking a write
        # surface into the TicketService constructor. Optional so
        # tests / non-API callers (e.g. the worker) can omit it.
        self.conv_repo = conv_repo
        self.default_minutes = sla_policy_default_minutes

    # ---- create ----

    async def create(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        subject: str,
        category: str | None = None,
        priority: TicketPriority = TicketPriority.P2,
    ) -> "Ticket":
        """Create a NEW ticket with an SLA deadline.

        SLA deadline is the **first-response** deadline — derived
        from the tenant's matching ``SlaPolicy.first_response_minutes``
        or, if no policy exists, the service's
        ``sla_policy_default_minutes``. The
        ``SlaPolicy.resolution_minutes`` field is captured separately
        (would require an additional column on ``Ticket``); for M1
        we expose only the first-response deadline.
        """
        sla = await self.repo.find_sla_policy(tenant_id, priority)
        sla_minutes = (
            sla.first_response_minutes if sla else self.default_minutes
        )
        sla_deadline = datetime.now(UTC) + timedelta(minutes=sla_minutes)
        ticket = await self.repo.create(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            subject=subject,
            category=category,
            priority=priority,
            sla_deadline_at=sla_deadline,
        )
        await self.repo.append_event(
            ticket_id=ticket.id,
            tenant_id=tenant_id,
            actor_type="system",
            actor_id=None,
            event_type="created",
            payload={
                "priority": priority.value,
                "sla_minutes": sla_minutes,
            },
        )
        log.info(
            "ticket_created",
            ticket_id=ticket.id,
            tenant_id=tenant_id,
            priority=priority.value,
        )
        return ticket

    # ---- transition ----

    async def transition(
        self,
        ticket_id: str,
        current: TicketStatus,
        target: TicketStatus,
        *,
        actor_type: str,
        actor_id: str | None,
        tenant_id: str,
    ) -> "Ticket":
        """Validate, load, mutate, audit, and log a state change.

        Order of operations matters:

        1. State machine guard (``sm_transition``) runs FIRST. An
           illegal transition raises :class:`InvalidTransition`
           without any DB write.
        2. Load the ticket scoped to ``tenant_id``. Cross-tenant /
           missing yields ``TicketNotFound``.
        3. Update status (with optional resolved/closed timestamps).
        4. If ``target == CANCELLED`` AND ``conv_repo`` was injected,
           NULL ``conversations.ticket_id`` (using the ticket repo's
           session so the cleanup is atomic with step 3) — otherwise
           deleting the ticket later would fail with a RESTRICT FK
           violation.
        5. Append the audit row.
        6. Log (opaque IDs only).
        """
        sm_transition(current, target)  # raises InvalidTransition

        ticket = await self.repo.get_by_id(ticket_id, tenant_id=tenant_id)
        if ticket is None:
            raise TicketNotFound(
                f"ticket {ticket_id} not found for tenant {tenant_id}"
            )

        now = datetime.now(UTC)
        resolved_at = now if target == TicketStatus.RESOLVED else None
        closed_at = now if target == TicketStatus.CLOSED else None
        await self.repo.update_status(
            ticket,
            target,
            tenant_id=tenant_id,
            resolved_at=resolved_at,
            closed_at=closed_at,
        )

        # CANCELLED cleanup: null the conversations.ticket_id back
        # pointer so the ticket can later be deleted without
        # tripping the RESTRICT FK. Threads the ticket repo's
        # session so the NULL write is atomic with the status
        # update — if the outer transaction rolls back, both writes
        # revert together. Best-effort — if no conv_repo was
        # injected (e.g. in a worker-driven flow), the caller is
        # responsible for cleaning up the conversation row.
        if target == TicketStatus.CANCELLED and self.conv_repo is not None:
            await self.conv_repo.clear_ticket_id(
                session=self.repo.session,
                conversation_id=ticket.conversation_id,
                tenant_id=ticket.tenant_id,
            )

        await self.repo.append_event(
            ticket_id=ticket_id,
            tenant_id=ticket.tenant_id,
            actor_type=actor_type,
            actor_id=actor_id,
            event_type="status_changed",
            payload={"from": current.value, "to": target.value},
        )
        log.info(
            "ticket_transitioned",
            ticket_id=ticket_id,
            tenant_id=ticket.tenant_id,
            actor_type=actor_type,
            from_=current.value,
            to=target.value,
        )
        return ticket

    # ---- lookup helpers ----

    async def get_or_create_for_conversation(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        subject: str,
    ) -> "Ticket":
        """Return the ticket attached to ``conversation_id`` or create one.

        Tenant-scoped: a conversation_id that belongs to another
        tenant is invisible — :meth:`TicketRepository.get_for_conversation`
        carries ``tenant_id`` in the WHERE clause, so this method
        transparently creates a fresh ticket for the new tenant
        without colliding on the unique conversation_id index.
        """
        existing = await self.repo.get_for_conversation(
            conversation_id, tenant_id=tenant_id
        )
        if existing is not None:
            return existing
        return await self.create(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            subject=subject,
        )


__all__ = ["TicketNotFound", "TicketService"]