"""Feishu (Lark) ChannelAdapter — inbound parsing only for M1.

Outbound sending is implemented in Task 4.8.
"""
import json
import logging
from datetime import UTC, datetime
from typing import Any

from channel.enums import ChannelType
from channel.messages import MessageEnvelope
from channel.models import Channel
from core.id_gen import new_id

logger = logging.getLogger(__name__)


class FeishuAdapter:
    """Parse Feishu event-callback v2 payloads into our MessageEnvelope."""

    channel_type = ChannelType.FEISHU

    async def parse_inbound(self, *, raw: dict[str, Any], channel: Channel) -> MessageEnvelope:
        """Convert a Feishu event v2 JSON into our MessageEnvelope.

        URL-verification events are handled by the webhook layer; this method
        expects `event_type == "event_callback"` and pulls sender/message data
        out of `event.{sender,message}`.
        """
        event = raw.get("event", {})
        sender = event.get("sender", {})
        sender_id = sender.get("sender_id", {})
        message = event.get("message", {})

        text = ""
        if message.get("message_type") == "text":
            content = message.get("content", "")
            try:
                parsed = json.loads(content)
            except (ValueError, TypeError):
                # Malformed content JSON — treat as empty rather than crash.
                logger.warning(
                    "feishu adapter: malformed content JSON",
                    extra={"message_id": message.get("message_id")},
                )
                parsed = {}
            if isinstance(parsed, dict):
                text_value = parsed.get("text", "")
                if isinstance(text_value, str):
                    text = text_value

        return MessageEnvelope(
            envelope_id=new_id(),
            tenant_id=channel.tenant_id,
            channel_type=ChannelType.FEISHU,
            channel_id=channel.id,
            external_conversation_id=message.get("chat_id", ""),
            external_user_id=sender_id.get("open_id", ""),
            external_message_id=message.get("message_id", ""),
            text=text,
            attachments=[],
            raw=raw,
            received_at=datetime.now(UTC),
        )

    async def send_outbound(self, *, envelope: MessageEnvelope, channel: Channel) -> None:
        """Send outbound via Feishu OpenAPI. Implemented in Task 4.8."""
        raise NotImplementedError(
            "Feishu outbound not yet implemented (Task 4.8)"
        )
