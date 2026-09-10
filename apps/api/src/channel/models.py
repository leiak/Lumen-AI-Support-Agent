"""ORM model for the transactional outbox.

We never call external systems synchronously when responding to events.
Instead, we INSERT a row into `outbox_events` inside the same transaction
as the state change. A background Worker drains the outbox and calls the
external system with retry + backoff.
"""
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from channel.enums import ChannelStatus, ChannelType
from core.database import Base

# All datetime columns store timezone-aware values (TIMESTAMP WITH TIME ZONE).
TZDateTime = DateTime(timezone=True)


class OutboxEvent(Base):
    """One row per pending external action (send message, run AI, etc.)."""

    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class Channel(Base):
    """A messaging channel configuration for a tenant.

    Holds credentials (encrypted in M2, plaintext JSON for M1) and per-channel config.
    """

    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[ChannelType] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    credentials_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ChannelStatus] = mapped_column(
        String(20), nullable=False, default=ChannelStatus.ACTIVE
    )
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ChannelBinding(Base):
    """Maps an external conversation (e.g., Feishu chat, Web Widget session)
    to an internal Conversation.

    `internal_conversation_id` references `conversations.id` which is created
    in Stage 5. For now, no FK constraint — Stage 5 will add the FK when
    that table exists.
    """

    __tablename__ = "channel_bindings"
    __table_args__ = (
        UniqueConstraint(
            "channel_id", "external_conversation_id", name="uq_channel_external_conv"
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    channel_id: Mapped[str] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_conversation_id: Mapped[str] = mapped_column(String(200), nullable=False)
    external_user_id: Mapped[str] = mapped_column(String(200), nullable=False)
    internal_conversation_id: Mapped[str] = mapped_column(String(26), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )