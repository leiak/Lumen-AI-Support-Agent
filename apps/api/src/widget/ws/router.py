"""WebSocket route for the embedded widget."""
from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from auth.jwt import TokenError
from channel.enums import ChannelStatus
from channel.repository import ChannelRepository
from core.id_gen import new_id
from widget.tokens import decode_widget_token
from widget.ws.manager import ConnectionManager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/widget", tags=["widget-ws"])

# Module-level singleton — single-process M1 deployment.
manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: Annotated[str, Query(min_length=1)],
) -> None:
    # 1. Authenticate
    try:
        payload = decode_widget_token(token)
    except TokenError as exc:
        logger.info("widget ws: rejected token: %s", exc)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    channel_id_raw = payload.get("channel_id")
    tenant_id_raw = payload.get("tenant_id")
    external_user_id_raw = payload.get("sub", "")

    if not isinstance(channel_id_raw, str) or not isinstance(tenant_id_raw, str):
        logger.info("widget ws: missing claims in widget token")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    channel_id: str = channel_id_raw
    tenant_id: str = tenant_id_raw
    external_user_id: str = external_user_id_raw if isinstance(external_user_id_raw, str) else ""

    # 2. Look up channel
    repo = ChannelRepository()
    channel = await repo.get_by_id(channel_id)
    if channel is None or channel.status != ChannelStatus.ACTIVE:
        logger.info(
            "widget ws: channel not active",
            extra={"channel_id": channel_id},
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # 3. Accept and register
    await websocket.accept()
    connection_id = await manager.connect(
        websocket=websocket,
        channel_id=channel_id,
        tenant_id=tenant_id,
        external_user_id=external_user_id,
    )

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "detail": "invalid JSON"})
                continue
            frame_type = frame.get("type")
            if frame_type == "ping":
                await websocket.send_json({"type": "pong"})
            elif frame_type == "typing":
                # No-op for M1; Stage 5 will broadcast typing events.
                pass
            elif frame_type == "message":
                # Defer persistence to Stage 5 (会话 + 消息).
                await websocket.send_json({
                    "type": "ack",
                    "external_message_id": new_id(),
                })
            else:
                await websocket.send_json(
                    {"type": "error", "detail": f"unknown type: {frame_type}"}
                )
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(connection_id)