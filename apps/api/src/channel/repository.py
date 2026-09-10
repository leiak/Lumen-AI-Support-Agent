"""Repository for Channel rows."""
import json
from typing import Any

from sqlalchemy import select

from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from core.database import get_session
from core.id_gen import new_id


class ChannelRepository:
    """CRUD for Channel rows."""

    async def create(
        self,
        *,
        tenant_id: str,
        type: ChannelType,
        name: str,
        credentials_encrypted: str,
        status: ChannelStatus = ChannelStatus.ACTIVE,
        config_json: dict[str, Any] | None = None,
    ) -> Channel:
        """Insert a new Channel. Returns the persisted Channel with id populated."""
        async with get_session() as session:
            channel = Channel(
                id=new_id(),
                tenant_id=tenant_id,
                type=type,
                name=name,
                credentials_encrypted=credentials_encrypted,
                status=status,
                config_json=config_json if config_json is not None else {},
            )
            session.add(channel)
            await session.flush()
            await session.refresh(channel)
            await session.commit()
            return channel

    async def get_by_id(self, channel_id: str) -> Channel | None:
        """Look up a Channel by primary key. Returns None if not found."""
        async with get_session() as session:
            return await session.get(Channel, channel_id)

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        type: ChannelType | None = None,
        status: ChannelStatus | None = None,
    ) -> list[Channel]:
        """Return channels for a tenant, optionally filtered by type/status."""
        async with get_session() as session:
            stmt = select(Channel).where(Channel.tenant_id == tenant_id)
            if type is not None:
                stmt = stmt.where(Channel.type == type)
            if status is not None:
                stmt = stmt.where(Channel.status == status)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def update_status(
        self,
        channel_id: str,
        status: ChannelStatus,
    ) -> Channel | None:
        """Update channel status. Returns updated row, or None if not found."""
        async with get_session() as session:
            channel = await session.get(Channel, channel_id)
            if channel is None:
                return None
            channel.status = status
            await session.commit()
            await session.refresh(channel)
            return channel

    async def update(
        self,
        channel_id: str,
        *,
        name: str | None = None,
        credentials_encrypted: str | None = None,
        status: ChannelStatus | None = None,
    ) -> Channel | None:
        """Patch a channel's mutable fields. Returns updated row, or None if not found.

        Only the fields that are explicitly provided (not None) are modified.
        The caller is expected to enforce tenant scoping before calling.
        """
        async with get_session() as session:
            channel = await session.get(Channel, channel_id)
            if channel is None:
                return None
            if name is not None:
                channel.name = name
            if credentials_encrypted is not None:
                channel.credentials_encrypted = credentials_encrypted
            if status is not None:
                channel.status = status
            await session.commit()
            await session.refresh(channel)
            return channel

    async def soft_delete(self, channel_id: str) -> Channel | None:
        """Soft-delete: flip status to DISABLED. Returns updated row, or None if not found."""
        return await self.update_status(channel_id, ChannelStatus.DISABLED)

    async def get_by_app_id(self, app_id: str) -> Channel | None:
        """Look up a FEISHU channel by its app_id stored in ``credentials_encrypted``.

        ``app_id`` is Feishu-specific — we filter to FEISHU channel type and
        scan the (small) set of FEISHU rows in M1, parsing the credentials
        JSON per row. Suitable for low-volume webhook lookup; revisit if the
        channel count grows large enough to warrant a dedicated column.

        Returns the matching Channel, or None if no FEISHU channel has that
        app_id. Channels whose ``credentials_encrypted`` is malformed JSON
        are silently skipped (logged at debug).
        """
        async with get_session() as session:
            stmt = select(Channel).where(Channel.type == ChannelType.FEISHU)
            result = await session.execute(stmt)
            for ch in result.scalars().all():
                try:
                    creds = json.loads(ch.credentials_encrypted)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(creds, dict) and creds.get("app_id") == app_id:
                    return ch
        return None