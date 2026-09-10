"""Repository layer for Conversation and Message ORM models.

Each method opens its own short-lived session via get_session(). This matches
the per-method pattern used in ChannelRepository and TenantRepository.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update

from conversation.enums import ConversationStatus
from conversation.models import Conversation, Message
from core.database import get_session


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
        limit: int = 50,
        offset: int = 0,
    ) -> list[Conversation]:
        """List Conversations for a tenant, optionally filtered by status."""
        async with get_session() as session:
            stmt = select(Conversation).where(Conversation.tenant_id == tenant_id)
            if status is not None:
                stmt = stmt.where(Conversation.status == status)
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
        limit: int = 50,
        offset: int = 0,
    ) -> list[Conversation]:
        """List Conversations assigned to a specific agent, optionally filtered by status."""
        async with get_session() as session:
            stmt = select(Conversation).where(
                Conversation.assigned_agent_id == assigned_agent_id
            )
            if status is not None:
                stmt = stmt.where(Conversation.status == status)
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