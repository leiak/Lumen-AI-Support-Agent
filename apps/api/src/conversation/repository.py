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
        """Insert a Conversation. Returns the same instance after commit."""
        async with get_session() as session:
            session.add(conversation)
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

    async def update(self, conversation: Conversation) -> Conversation:
        """Persist mutations on a Conversation. Returns the same instance."""
        async with get_session() as session:
            session.add(conversation)
            await session.commit()
            return conversation

    async def touch_last_activity(self, *, conversation_id: str, at: datetime) -> None:
        """Update last_activity_at without loading the row."""
        async with get_session() as session:
            stmt = (
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(last_activity_at=at)
            )
            await session.execute(stmt)


class MessageRepository:
    """CRUD for Message rows."""

    async def create(self, *, message: Message) -> Message:
        """Insert a Message. Returns the same instance after commit."""
        async with get_session() as session:
            session.add(message)
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

        If ``before`` is supplied, only messages strictly older than that
        timestamp are returned (for pagination).
        """
        async with get_session() as session:
            stmt = (
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.asc())
                .limit(limit)
            )
            if before is not None:
                stmt = stmt.where(Message.created_at < before)
            result = await session.execute(stmt)
            return list(result.scalars().all())

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