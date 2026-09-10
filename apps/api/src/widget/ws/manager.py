"""In-process WebSocket connection manager.

For M1 single-process deployments only. Multi-worker fanout via Redis pub/sub
is Stage 5+ (see Stage 5 plan: 会话 + 消息).

Caveats:
- Read methods (list_connections, count, get_state) do not hold the lock.
  In CPython this is safe from corruption thanks to the GIL, but readers
  may observe a half-completed state during concurrent connect/disconnect.
  Acceptable for M1 observability use cases; not safe for atomic snapshots.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.id_gen import new_id

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fastapi import WebSocket


@dataclass
class ConnectionState:
    connection_id: str
    channel_id: str
    tenant_id: str
    external_user_id: str
    connected_at: datetime


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._states: dict[str, ConnectionState] = {}
        self._lock = asyncio.Lock()

    async def connect(
        self,
        *,
        websocket: WebSocket,
        channel_id: str,
        tenant_id: str,
        external_user_id: str,
    ) -> str:
        connection_id = new_id()
        async with self._lock:
            self._connections[connection_id] = websocket
            self._states[connection_id] = ConnectionState(
                connection_id=connection_id,
                channel_id=channel_id,
                tenant_id=tenant_id,
                external_user_id=external_user_id,
                connected_at=datetime.now(UTC),
            )
        return connection_id

    async def disconnect(self, connection_id: str) -> None:
        async with self._lock:
            self._connections.pop(connection_id, None)
            self._states.pop(connection_id, None)

    def get_state(self, connection_id: str) -> ConnectionState | None:
        return self._states.get(connection_id)

    def list_connections(self, channel_id: str) -> list[ConnectionState]:
        return [s for s in self._states.values() if s.channel_id == channel_id]

    def count(self) -> int:
        return len(self._connections)

    async def send_to_connection(self, connection_id: str, payload: dict[str, Any]) -> bool:
        ws = self._connections.get(connection_id)
        if ws is None:
            return False
        await ws.send_json(payload)
        return True

    async def broadcast_to_channel(
        self,
        channel_id: str,
        payload: dict[str, Any],
        *,
        exclude: str | None = None,
    ) -> int:
        """Fan out ``payload`` to every connection on ``channel_id``.

        Returns the number of connections that successfully received the
        frame. If any target's socket raises mid-delivery, the failure is
        logged and the remaining targets are still attempted — one dead
        socket does not abort the fanout.
        """
        targets = [
            cid for cid, state in self._states.items()
            if state.channel_id == channel_id and cid != exclude
        ]
        delivered = 0
        for cid in targets:
            try:
                ok = await self.send_to_connection(cid, payload)
            except Exception:
                logger.warning(
                    "widget ws: broadcast send failed",
                    extra={"connection_id": cid},
                )
                continue
            if ok:
                delivered += 1
        return delivered


# Process-wide singleton — single-process M1 deployment.
#
# This MUST be the one and only instance: `widget.ws.router` owns the
# connection lifecycle (connect/disconnect) while `channel.inbound` performs
# server-initiated broadcasts. If each module instantiated its own
# ConnectionManager they would hold separate connection tables and every
# broadcast would silently deliver to zero clients.
#
# It lives here rather than in `widget.ws.router` because `router` imports
# `channel.inbound.process_inbound_envelope`; importing back the other way
# would be a circular import. This module depends on nothing but core.id_gen.
manager = ConnectionManager()
