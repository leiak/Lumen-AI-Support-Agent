"""Repository for Channel rows."""
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