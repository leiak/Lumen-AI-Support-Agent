"""In-process WebSocket connection manager.

For M1 single-process deployments only. Multi-worker fanout via Redis pub/sub
is Stage 5+ (see Stage 5 plan: 会话 + 消息).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.id_gen import new_id

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
        targets = [
            cid for cid, state in self._states.items()
            if state.channel_id == channel_id and cid != exclude
        ]
        delivered = 0
        for cid in targets:
            ok = await self.send_to_connection(cid, payload)
            if ok:
                delivered += 1
        return delivered