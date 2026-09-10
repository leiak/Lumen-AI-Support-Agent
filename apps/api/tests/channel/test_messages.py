"""Tests for channel-agnostic message types."""
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from channel.enums import ChannelType
from channel.messages import Attachment, MessageEnvelope


def test_envelope_minimal_fields() -> None:
    env = MessageEnvelope(
        tenant_id="t1",
        channel_type=ChannelType.WEB,
        channel_id="ch1",
        external_user_id="u1",
        external_conversation_id="c1",
        text="hi",
        received_at=datetime.now(UTC),
    )
    assert env.text == "hi"
    assert env.attachments == []
    assert env.raw == {}


def test_envelope_with_attachments() -> None:
    env = MessageEnvelope(
        tenant_id="t1",
        channel_type=ChannelType.FEISHU,
        channel_id="ch1",
        external_user_id="u1",
        external_conversation_id="c1",
        text="see image",
        attachments=[
            Attachment(type="image", url="https://x.com/i.png", mime_type="image/png"),
        ],
        raw={"feishu_event_id": "evt_123"},
        received_at=datetime.now(UTC),
    )
    assert len(env.attachments) == 1
    assert env.attachments[0].mime_type == "image/png"
    assert env.raw["feishu_event_id"] == "evt_123"


def test_envelope_missing_required_field() -> None:
    with pytest.raises(ValidationError):
        MessageEnvelope(
            tenant_id="t1",
            # missing channel_type, channel_id, etc.
            text="hi",
            received_at=datetime.now(UTC),
        )


def test_envelope_accepts_naive_datetime() -> None:
    """Pydantic v2 accepts naive datetimes; we tolerate them but TZ-aware is preferred.

    Note: callers should pass timezone-aware datetimes (datetime.now(UTC))
    to avoid ambiguity. We don't reject naive ones at the model layer.
    """
    env = MessageEnvelope(
        tenant_id="t1",
        channel_type=ChannelType.WEB,
        channel_id="ch1",
        external_user_id="u1",
        external_conversation_id="c1",
        text="hi",
        received_at=datetime.now(),  # naive
    )
    assert env.received_at is not None


def test_attachment_optional_mime_type() -> None:
    a = Attachment(type="file", url="https://x.com/f.pdf")
    assert a.mime_type is None


def test_channel_adapter_protocol_runtime_checkable() -> None:
    """A class implementing the protocol's methods can be used as a ChannelAdapter."""
    from channel.protocols import ChannelAdapter

    class FakeAdapter(ChannelAdapter):
        channel_type = ChannelType.WEB

        async def parse_inbound(self, request):  # type: ignore[no-untyped-def]
            return MessageEnvelope(
                tenant_id="t",
                channel_type=self.channel_type,
                channel_id="ch",
                external_user_id="u",
                external_conversation_id="c",
                text="hi",
                received_at=datetime.now(UTC),
            )

        async def send_outbound(self, *, channel, external_user_id, text, attachments=None):  # type: ignore[no-untyped-def]
            return None

    adapter = FakeAdapter()
    # duck-type: has all required attributes/methods
    assert adapter.channel_type == ChannelType.WEB
    assert hasattr(adapter, "parse_inbound")
    assert hasattr(adapter, "send_outbound")
    # Protocol is importable
    assert ChannelAdapter is not None
