"""WebSocket route for the embedded widget."""
from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from auth.jwt import TokenError
from channel.enums import ChannelStatus
from channel.inbound import process_inbound_envelope
from channel.repository import ChannelRepository
from widget.adapter import WebWidgetAdapter
from widget.tokens import decode_widget_token
from widget.ws.manager import manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/widget", tags=["widget-ws"])

__all__ = ["manager", "router", "websocket_endpoint"]


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: Annotated[str, Query(min_length=1)],
) -> None:
    # 1. Authenticate
    try:
        payload = decode_widget_token(token)
    except TokenError as exc:
        logger.warning("widget ws: rejected token: %s", exc)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    channel_id = payload.get("channel_id")
    tenant_id = payload.get("tenant_id")
    if (
        not isinstance(channel_id, str)
        or not channel_id
        or not isinstance(tenant_id, str)
        or not tenant_id
    ):
        logger.warning("widget ws: token missing channel_id or tenant_id")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    external_user_id = payload.get("sub")
    if not isinstance(external_user_id, str) or not external_user_id:
        logger.warning("widget ws: token missing sub claim")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # 2. Look up channel
    repo = ChannelRepository()
    channel = await repo.get_by_id(channel_id)
    if channel is None or channel.status != ChannelStatus.ACTIVE:
        logger.warning(
            "widget ws: channel not active",
            extra={"channel_id": channel_id},
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # 2b. Tenant cross-use enforcement: token's tenant must match channel's tenant.
    if channel.tenant_id != tenant_id:
        logger.warning(
            "widget ws: tenant mismatch",
            extra={"token_tenant_id": tenant_id, "channel_tenant_id": channel.tenant_id},
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
                # Parse via the widget adapter, persist via the channel-agnostic
                # inbound processor, then ACK with the resulting conversation_id
                # so the frontend can refresh its state. Full WS protocol
                # (broadcast envelope, etc.) is a Stage 5+ concern.
                adapter = WebWidgetAdapter()
                envelope = await adapter.parse_inbound(raw=frame, channel=channel)
                await process_inbound_envelope(envelope)
                await websocket.send_json({
                    "type": "ack",
                    "external_message_id": envelope.envelope_id,
                })
            else:
                await websocket.send_json(
                    {"type": "error", "detail": f"unknown type: {frame_type}"}
                )
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(connection_id)