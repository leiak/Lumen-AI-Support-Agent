"""Repository layer for Conversation and Message ORM models.

Each method opens its own short-lived session via get_session(). This matches
the per-method pattern used in ChannelRepository and TenantRepository.

``SELECT ... FOR UPDATE``
-------------------------

Two methods accept an externally-owned ``AsyncSession`` so the caller can
wrap the row lock + read + write in a single transaction:

* :meth:`ConversationRepository.get_by_id_for_update` — locks a
  single Conversation row (used by :meth:`ConversationService.claim`
  so two concurrent claim attempts serialize at the DB level).
* :meth:`MessageRepository.<reserved for future>` — not yet added.

Mirrors the ``session``-parameter pattern in
``knowledge/repository.py::ArticleRepository.get_by_id_for_update``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from conversation.enums import ConversationStatus
from conversation.models import Conversation, Message
from core.database import get_session


def _search_filter(value: str) -> Any:
    """Build a case-insensitive ILIKE filter over id / customer_external_id."""
    needle = value.lower()
    return or_(
        func.lower(Conversation.id).contains(needle),
        func.lower(Conversation.customer_external_id).contains(needle),
    )


class ConversationRepository:
    """CRUD for Conversation rows."""

    async def create(self, *, conversation: Conversation) -> Conversation:
        """Insert a Conversation. Returns the same instance after commit.

        Flushes + refreshes so DB-generated defaults (e.g. server-side
        timestamps) are populated on the returned instance.
        """
        async with get_session() as session:
            session.add(conversation)
            await session.flush()
            await session.refresh(conversation)
            await session.commit()
            return conversation

    async def get_by_id(self, conversation_id: str) -> Conversation | None:
        """Look up a Conversation by primary key. Returns None if not found."""
        async with get_session() as session:
            return await session.get(Conversation, conversation_id)

    async def find_open_by_channel_customer(
        self, *, channel_id: str, customer_external_id: str
    ) -> Conversation | None:
        """Find the most recent non-CLOSED Conversation for a (channel, customer) pair.

        Returns None when no such conversation exists.
        """
        async with get_session() as session:
            stmt = (
                select(Conversation)
                .where(Conversation.channel_id == channel_id)
                .where(Conversation.customer_external_id == customer_external_id)
                .where(Conversation.status != ConversationStatus.CLOSED)
                .order_by(Conversation.last_activity_at.desc())
            )
            result = await session.execute(stmt)
            return result.scalars().first()

    async def list_by_tenant(
        self,
        *,
        tenant_id: str,
        status: ConversationStatus | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Conversation]:
        """List Conversations for a tenant, optionally filtered by status / search."""
        async with get_session() as session:
            stmt = select(Conversation).where(Conversation.tenant_id == tenant_id)
            if status is not None:
                stmt = stmt.where(Conversation.status == status)
            if search:
                stmt = stmt.where(_search_filter(search))
            stmt = (
                stmt.order_by(Conversation.last_activity_at.desc())
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def list_by_assigned_agent(
        self,
        *,
        assigned_agent_id: str,
        status: ConversationStatus | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Conversation]:
        """List Conversations assigned to a specific agent, optionally filtered."""
        async with get_session() as session:
            stmt = select(Conversation).where(
                Conversation.assigned_agent_id == assigned_agent_id
            )
            if status is not None:
                stmt = stmt.where(Conversation.status == status)
            if search:
                stmt = stmt.where(_search_filter(search))
            stmt = (
                stmt.order_by(Conversation.last_activity_at.desc())
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def update(self, conversation: Conversation) -> Conversation | None:
        """Persist mutations on a Conversation.

        Loads the row by primary key to avoid stale-detached-state issues,
        then applies the mutable attributes and refreshes the persisted
        instance. Returns None when the row does not exist.
        """
        async with get_session() as session:
            existing = await session.get(Conversation, conversation.id)
            if existing is None:
                return None
            existing.status = conversation.status
            existing.assigned_agent_id = conversation.assigned_agent_id
            existing.ai_handling = conversation.ai_handling
            existing.last_activity_at = conversation.last_activity_at
            await session.flush()
            await session.refresh(existing)
            await session.commit()
            return existing

    async def touch_last_activity(self, *, conversation_id: str, at: datetime) -> None:
        """Update last_activity_at without loading the row.

        No-op when the conversation does not exist. Callers should
        pre-validate ``conversation_id`` exists; this method silently
        skips missing rows to avoid throwing in the message-write hot loop.
        """
        async with get_session() as session:
            stmt = (
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(last_activity_at=at)
            )
            await session.execute(stmt)
            await session.commit()

    async def list_pending_for_tenant(
        self,
        *,
        tenant_id: str,
        status: ConversationStatus = ConversationStatus.PENDING,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Conversation]:
        """List Conversations for a tenant, filtered by status, newest-activity first.

        Two modes, selected by the ``status`` argument:

        * ``status == PENDING`` (default — the agent workspace's
          "queue" view): returns PENDING conversations with
          ``assigned_agent_id IS NULL`` — i.e. the unassigned work
          waiting for someone to claim.
        * Any other status: returns all conversations with that
          status regardless of whether they're assigned. Used by
          admin reporting views.

        Sorted by ``last_activity_at DESC`` so the freshest activity
        surfaces first; pagination via ``limit`` / ``offset``.

        Anti-enumeration note: this method always carries
        ``tenant_id`` in the WHERE clause. A cross-tenant caller
        simply gets their own queue, not someone else's.
        """
        async with get_session() as session:
            stmt = select(Conversation).where(
                Conversation.tenant_id == tenant_id,
                Conversation.status == status,
            )
            if status == ConversationStatus.PENDING:
                # The agent workspace's "unassigned queue" filter:
                # only PENDING + unassigned. Admins picking a non-
                # PENDING status get all rows for that status
                # regardless of assignment.
                stmt = stmt.where(Conversation.assigned_agent_id.is_(None))
            stmt = (
                stmt.order_by(Conversation.last_activity_at.desc())
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def clear_ticket_id(
        self, *, conversation_id: str, tenant_id: str
    ) -> bool:
        """NULL the back-pointer ``conversations.ticket_id``.

        Called by :class:`ticket.service.TicketService` when a ticket
        is transitioned to ``CANCELLED`` — the RESTRICT FK on
        ``conversations.ticket_id`` (declared in Task 4) blocks any
        future DELETE of the ticket while the conversation still
        points at it, so we MUST null the back-pointer on cancel.
        Tenant-scoped on the WHERE clause so a foreign
        ``conversation_id`` is silently ignored (returns
        ``rowcount == 0``).

        Returns ``True`` when a row was updated, ``False`` otherwise
        (missing row OR cross-tenant). The service treats the
        ``False`` return as a soft no-op — the caller (Task 6) is
        not informed.
        """
        async with get_session() as session:
            stmt = (
                update(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.tenant_id == tenant_id,
                )
                .values(ticket_id=None)
            )
            result = await session.execute(stmt)
            return result.rowcount > 0

    async def get_by_id_for_update(
        self,
        *,
        session: AsyncSession,
        tenant_id: str,
        conversation_id: str,
    ) -> Conversation | None:
        """Look up a Conversation scoped to ``tenant_id`` with ``SELECT ... FOR UPDATE``.

        Used by :meth:`ConversationService.claim` so two concurrent
        claim attempts on the same row serialize at the DB level:
        the second caller blocks on the row lock until the first
        commits, then reads the freshly-updated
        ``assigned_agent_id`` and short-circuits to a 409.

        The caller owns ``session`` and is responsible for
        ``commit()`` / ``rollback()`` — this method only acquires
        the row lock within the caller's transaction.

        Returns ``None`` if the row doesn't exist OR belongs to a
        different tenant — anti-enumeration parity with
        :meth:`get_by_id` (same WHERE clause shape, just under a
        row lock).
        """
        stmt = (
            select(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        return (await session.execute(stmt)).scalar_one_or_none()


class MessageRepository:
    """CRUD for Message rows."""

    async def create(self, *, message: Message) -> Message:
        """Insert a Message. Returns the same instance after commit.

        Flushes + refreshes so DB-generated defaults are populated on the
        returned instance.
        """
        async with get_session() as session:
            session.add(message)
            await session.flush()
            await session.refresh(message)
            await session.commit()
            return message

    async def get_by_id(self, message_id: str) -> Message | None:
        """Look up a Message by primary key. Returns None if not found."""
        async with get_session() as session:
            return await session.get(Message, message_id)

    async def list_by_conversation(
        self,
        *,
        conversation_id: str,
        limit: int = 100,
        before: datetime | None = None,
    ) -> list[Message]:
        """List Messages for a conversation in chronological order.

        Default (no ``before``): return the latest ``limit`` messages in
        ASCENDING chronological order, so the consumer can render them as
        a timeline without re-sorting.

        With ``before``: return the ``limit`` messages immediately
        preceding the cursor in ASCENDING chronological order, enabling
        backward cursor pagination.
        """
        async with get_session() as session:
            if before is not None:
                stmt = (
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .where(Message.created_at < before)
                    .order_by(Message.created_at.desc())
                    .limit(limit)
                )
            else:
                stmt = (
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.created_at.desc())
                    .limit(limit)
                )
            result = await session.execute(stmt)
            return list(reversed(result.scalars().all()))

    async def count_by_conversation(self, *, conversation_id: str) -> int:
        """Return the total number of Messages attached to a conversation."""
        async with get_session() as session:
            stmt = (
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == conversation_id)
            )
            result = await session.execute(stmt)
            return int(result.scalar() or 0)