"""Protocols (interfaces) that channel adapters must implement."""
from typing import Protocol

from fastapi import Request

from channel.enums import ChannelType
from channel.messages import Attachment, MessageEnvelope
from channel.models import Channel


class ChannelAdapter(Protocol):
    """Interface every channel adapter must implement.

    An adapter is responsible for:
    - parsing inbound HTTP requests from the channel into a MessageEnvelope
    - sending outbound messages to the channel via the provider's API
    """

    channel_type: ChannelType

    async def parse_inbound(self, request: Request) -> MessageEnvelope:
        """Parse an inbound HTTP request from the channel into a MessageEnvelope.

        Validates signatures (e.g., Feishu X-Lark-Signature), extracts user/chat IDs,
        fetches message content from the provider API if needed.
        """

    async def send_outbound(
        self,
        *,
        channel: Channel,
        external_user_id: str,
        text: str,
        attachments: list[Attachment] | None = None,
    ) -> None:
        """Send a message to the external user via the channel.

        ``channel`` is the Channel ORM row (carries credentials).
        ``external_user_id`` is the user's ID in the external system.
        """
