"""Tests for the WebWidgetAdapter."""
from datetime import UTC, datetime

import pytest

from channel.enums import ChannelStatus, ChannelType
from channel.messages import MessageEnvelope
from channel.models import Channel
from core.id_gen import new_id
from widget.adapter import WebWidgetAdapter


def _channel() -> Channel:
    return Channel(
        id=new_id(),
        tenant_id=new_id(),
        type=ChannelType.WEB,
        name="x",
        status=ChannelStatus.ACTIVE,
        credentials_encrypted="{}",
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_parse_message_frame_returns_envelope() -> None:
    raw = {
        "type": "message",
        "conversation_id": "conv_1",
        "external_user_id": "u_42",
        "client_message_id": "client_msg_abc",
        "text": "hello",
    }
    envelope = await WebWidgetAdapter().parse_inbound(raw=raw, channel=_channel())
    assert envelope.channel_type == ChannelType.WEB
    assert envelope.external_conversation_id == "conv_1"
    assert envelope.external_user_id == "u_42"
    assert envelope.external_message_id == "client_msg_abc"
    assert envelope.text == "hello"
    assert envelope.attachments == []


@pytest.mark.asyncio
async def test_parse_typing_frame_returns_empty_text() -> None:
    raw = {"type": "typing", "value": True}
    envelope = await WebWidgetAdapter().parse_inbound(raw=raw, channel=_channel())
    assert envelope.text == ""


@pytest.mark.asyncio
async def test_parse_unknown_frame_does_not_crash() -> None:
    raw = {"type": "garbage"}
    envelope = await WebWidgetAdapter().parse_inbound(raw=raw, channel=_channel())
    assert envelope.text == ""


@pytest.mark.asyncio
async def test_send_outbound_broadcasts_to_channel(monkeypatch) -> None:
    """send_outbound should call manager.broadcast_to_channel with the right args."""
    captured: dict = {}

    async def fake_broadcast(channel_id, payload, *, exclude=None):
        captured["channel_id"] = channel_id
        captured["payload"] = payload
        return 1

    from widget.ws import router
    monkeypatch.setattr(router.manager, "broadcast_to_channel", fake_broadcast)

    channel = _channel()
    envelope = MessageEnvelope(
        envelope_id=new_id(),
        tenant_id=channel.tenant_id,
        channel_type=ChannelType.WEB,
        channel_id=channel.id,
        external_conversation_id="conv_1",
        external_user_id="u_42",
        external_message_id="om_1",
        text="reply",
        attachments=[],
        raw={},
        received_at=datetime.now(UTC),
    )
    await WebWidgetAdapter().send_outbound(envelope=envelope, channel=channel)
    assert captured["channel_id"] == channel.id
    assert captured["payload"]["type"] == "message"
    assert captured["payload"]["text"] == "reply"
    assert captured["payload"]["external_message_id"] == "om_1"


def test_adapter_channel_type() -> None:
    assert WebWidgetAdapter().channel_type == ChannelType.WEB
