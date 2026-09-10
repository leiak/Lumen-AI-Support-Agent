"""Feishu (Lark) ChannelAdapter — inbound parsing + outbound via OpenAPI."""
import json
import logging
from datetime import UTC, datetime
from typing import Any, ClassVar

from channel.enums import ChannelType
from channel.feishu.client import FeishuOpenAPIClient
from channel.messages import MessageEnvelope
from channel.models import Channel
from core.id_gen import new_id

logger = logging.getLogger(__name__)


class FeishuAdapter:
    """Parse Feishu event-callback v2 payloads into our MessageEnvelope, and
    deliver outbound text messages via the Feishu OpenAPI.
    """

    channel_type = ChannelType.FEISHU
    _client: ClassVar[FeishuOpenAPIClient] = FeishuOpenAPIClient()

    @classmethod
    def reset_client(cls) -> None:
        """Test helper — reset the singleton client + its token cache."""
        cls._client = FeishuOpenAPIClient()

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
        """Deliver an outbound envelope to the Feishu user via OpenAPI.

        Credentials (app_id / app_secret) are read from the channel's
        `credentials_encrypted` JSON text column. The OpenAPI client is a
        class-level singleton that caches `tenant_access_token` per app_id.
        """
        creds = json.loads(channel.credentials_encrypted)
        await self._client.send_text_message(
            app_id=creds["app_id"],
            app_secret=creds["app_secret"],
            receive_id=envelope.external_user_id,
            text=envelope.text,
        )
