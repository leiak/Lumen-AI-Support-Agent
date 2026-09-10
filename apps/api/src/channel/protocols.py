"""Protocols (interfaces) that channel adapters must implement."""
from typing import Any, Protocol

from channel.enums import ChannelType
from channel.messages import MessageEnvelope
from channel.models import Channel


class ChannelAdapter(Protocol):
    """Interface every channel adapter must implement.

    An adapter is responsible for:
    - parsing inbound payloads from the channel into a MessageEnvelope
    - sending outbound messages to the channel via the provider's API

    Signature/verification and decryption of inbound HTTP bodies is handled by
    the channel's webhook HTTP layer; adapters operate on already-decoded
    JSON dicts.
    """

    channel_type: ChannelType

    async def parse_inbound(self, *, raw: dict[str, Any], channel: Channel) -> MessageEnvelope:
        """Convert an already-verified, decrypted provider event JSON into a MessageEnvelope.

        ``raw`` is the parsed JSON body from the channel's webhook.
        ``channel`` is the Channel ORM row (carries tenant_id, credentials, etc.).

        Implementations are responsible for populating ``envelope_id`` (a fresh
        ULID/UUID for the in-app message) and ``external_message_id`` (the
        provider's own message identifier) on the returned envelope.
        """

    async def send_outbound(
        self,
        *,
        envelope: MessageEnvelope,
        channel: Channel,
    ) -> None:
        """Deliver an outbound envelope to the external user via the channel.

        ``envelope`` carries the text/attachments plus the destination identifiers
        (external_user_id, external_conversation_id). ``channel`` carries the
        credentials used to authenticate with the provider API.
        """
