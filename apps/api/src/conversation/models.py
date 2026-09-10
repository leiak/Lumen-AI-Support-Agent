"""ORM models for conversations and messages."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from conversation.enums import ConversationStatus, MessageRole
from core.database import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[ConversationStatus] = mapped_column(
        String(20),
        nullable=False,
        default=ConversationStatus.OPEN,
    )
    assigned_agent_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    ai_handling: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "customer_external_id",
            name="uq_conversations_channel_customer",
        ),
        Index("ix_conversations_tenant_status", "tenant_id", "status"),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[MessageRole] = mapped_column(String(20), nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_blocks_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    sender_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    tool_calls_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )
