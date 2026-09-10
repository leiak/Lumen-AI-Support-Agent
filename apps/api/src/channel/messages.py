"""Channel-agnostic message types — the wire format between channels and the rest of the system."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from channel.enums import ChannelType
from core.id_gen import new_id


class Attachment(BaseModel):
    """A file attached to a message."""

    type: str  # "image", "file", "audio", "video"
    url: str
    mime_type: str | None = None


class MessageEnvelope(BaseModel):
    """A parsed inbound or outbound message in our channel-agnostic format.

    Inbound: produced by a ChannelAdapter.parse_inbound() from an HTTP request.
    Outbound: sent into a ChannelAdapter.send_outbound() to deliver to the external system.
    """

    # Auto-generated unique id for this envelope — used to dedupe inbound retries
    # and to correlate the envelope with downstream state (sessions, messages, etc.).
    envelope_id: str = Field(default_factory=new_id)
    tenant_id: str
    channel_type: ChannelType
    channel_id: str
    external_user_id: str
    external_conversation_id: str
    # Provider-side message id (Feishu's om_*, Slack's ts, etc.). Empty for
    # outbound envelopes where the provider has not yet assigned an id.
    external_message_id: str = ""
    text: str
    attachments: list[Attachment] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime
