"""Web Widget ChannelAdapter.

Converts widget WebSocket frames into our MessageEnvelope (inbound), and pushes
outbound envelopes to live WebSocket connections via the in-process
ConnectionManager (outbound).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from channel.enums import ChannelType
from channel.messages import MessageEnvelope
from channel.models import Channel
from core.id_gen import new_id

logger = logging.getLogger(__name__)


class WebWidgetAdapter:
    channel_type = ChannelType.WEB

    async def parse_inbound(self, *, raw: dict[str, Any], channel: Channel) -> MessageEnvelope:
        """Convert a widget frame into our MessageEnvelope.

        Defensive on frame_type — only ``message`` frames carry text. ``typing``
        and other frames yield an empty text envelope so downstream code can
        handle them uniformly without crashing.
        """
        frame_type = raw.get("type", "")
        text = raw.get("text", "") if frame_type == "message" else ""
        return MessageEnvelope(
            envelope_id=new_id(),
            tenant_id=channel.tenant_id,
            channel_type=ChannelType.WEB,
            channel_id=channel.id,
            external_conversation_id=raw.get("conversation_id", ""),
            external_user_id=raw.get("external_user_id", ""),
            external_message_id=raw.get("client_message_id", ""),
            text=text,
            attachments=[],
            raw=raw,
            received_at=datetime.now(UTC),
        )

    async def send_outbound(self, *, envelope: MessageEnvelope, channel: Channel) -> None:
        """Push an outbound envelope to the live WebSocket connection(s) for the channel.

        For M1 we broadcast by ``channel_id`` — all live connections for the
        channel receive the frame. Stage 5 will refine targeting by conversation
        once ``external_conversation_id`` is plumbed through the WS router.
        """
        # Lazy import to avoid circular dependency at module load time.
        from widget.ws.router import manager

        payload = {
            "type": "message",
            "external_message_id": envelope.external_message_id or new_id(),
            "text": envelope.text,
            "channel_type": ChannelType.WEB.value,
        }
        delivered = await manager.broadcast_to_channel(channel.id, payload)
        if delivered == 0:
            logger.info(
                "widget adapter: no live connections for outbound",
                extra={
                    "channel_id": channel.id,
                    "envelope_id": envelope.envelope_id,
                },
            )
