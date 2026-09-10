"""Tenant-scoped channel management service.

This is the business-logic layer between the channel management HTTP API
(`channel/api.py`) and the persistence layer (`channel/repository.py`).
The service is responsible for:

- enforcing tenant scoping on every operation (a channel from another
  tenant must never leak through `get`/`update`/`soft_delete`);
- serializing credentials to a JSON string before persisting (the column
  is `Text`, not `JSONB`, in M1 — encryption at rest is an M2 concern);
- returning `None` for not-found / cross-tenant access rather than
  raising, so the API layer can map to 404 uniformly.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from channel.repository import ChannelRepository

logger = logging.getLogger(__name__)


class ChannelService:
    """Tenant-scoped CRUD service for Channel rows."""

    def __init__(self, repo: ChannelRepository | None = None) -> None:
        self._repo = repo or ChannelRepository()

    async def create(
        self,
        *,
        tenant_id: str,
        type: ChannelType,
        name: str,
        credentials: dict[str, Any],
    ) -> Channel:
        """Create a new ACTIVE channel for the tenant."""
        credentials_encrypted = json.dumps(credentials)
        return await self._repo.create(
            tenant_id=tenant_id,
            type=type,
            name=name,
            credentials_encrypted=credentials_encrypted,
            status=ChannelStatus.ACTIVE,
        )

    async def list(self, *, tenant_id: str) -> list[Channel]:
        """Return all channels for the tenant (no pagination in M1)."""
        return await self._repo.list_for_tenant(tenant_id)

    async def get(self, *, tenant_id: str, channel_id: str) -> Channel | None:
        """Return the channel if it belongs to the tenant; otherwise None.

        Returning `None` for both "not found" and "wrong tenant" prevents
        tenant enumeration via response timing/status differences — the
        API layer maps both cases to 404.
        """
        channel = await self._repo.get_by_id(channel_id)
        if channel is None or channel.tenant_id != tenant_id:
            return None
        return channel

    async def update(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        name: str | None = None,
        status: ChannelStatus | None = None,
        credentials: dict[str, Any] | None = None,
    ) -> Channel | None:
        """Patch mutable fields on a tenant-owned channel.

        Any argument left as `None` is left unchanged. Credentials are
        re-serialized to JSON; this overwrites any prior stored value.
        """
        existing = await self.get(tenant_id=tenant_id, channel_id=channel_id)
        if existing is None:
            return None
        credentials_encrypted: str | None = None
        if credentials is not None:
            credentials_encrypted = json.dumps(credentials)
        return await self._repo.update(
            channel_id,
            name=name,
            credentials_encrypted=credentials_encrypted,
            status=status,
        )

    async def soft_delete(self, *, tenant_id: str, channel_id: str) -> Channel | None:
        """Soft-disable a tenant-owned channel (status -> DISABLED)."""
        existing = await self.get(tenant_id=tenant_id, channel_id=channel_id)
        if existing is None:
            return None
        return await self._repo.soft_delete(channel_id)