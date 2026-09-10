"""Tests for the Feishu ChannelAdapter."""
import json
from datetime import UTC, datetime

import pytest

from channel.enums import ChannelStatus, ChannelType
from channel.feishu.adapter import FeishuAdapter
from channel.feishu.exceptions import FeishuAPIError
from channel.messages import MessageEnvelope
from channel.models import Channel
from core.id_gen import new_id


def _feishu_text_event(
    *,
    chat_id: str = "oc_test_chat",
    open_id: str = "ou_test_user",
    message_id: str = "om_test_msg",
    text: str = "hello",
) -> dict:
    return {
        "schema": "2.0",
        "header": {
            "event_type": "im.message.receive_v1",
            "app_id": "cli_test_app",
            "tenant_key": "test_tenant",
            "event_id": "ev_test_event",
            "create_time": "1700000000000",
        },
        "event": {
            "sender": {
                "sender_id": {
                    "open_id": open_id,
                    "union_id": "on_" + open_id,
                    "user_id": "u_" + open_id,
                }
            },
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": "p2p",
                "message_type": "text",
                "content": '{"text":"' + text + '"}',
            },
        },
    }


def _channel(*, encrypt_key: str = "", app_id: str = "cli_test_app") -> Channel:
    """Build a Channel ORM-like instance for unit tests (no DB needed).

    `credentials_encrypted` is stored as JSON text per the model schema.
    """
    creds = json.dumps(
        {
            "app_id": app_id,
            "app_secret": "secret",
            "verification_token": "token",
            "encrypt_key": encrypt_key,
        }
    )
    return Channel(
        id=new_id(),
        tenant_id=new_id(),
        type=ChannelType.FEISHU,
        name="test-feishu",
        status=ChannelStatus.ACTIVE,
        credentials_encrypted=creds,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_parse_text_event_returns_envelope() -> None:
    adapter = FeishuAdapter()
    raw = _feishu_text_event(text="hi there")
    channel = _channel()
    envelope = await adapter.parse_inbound(raw=raw, channel=channel)
    assert isinstance(envelope, MessageEnvelope)
    assert envelope.external_conversation_id == "oc_test_chat"
    assert envelope.external_user_id == "ou_test_user"
    assert envelope.external_message_id == "om_test_msg"
    assert envelope.text == "hi there"
    assert envelope.attachments == []
    assert envelope.raw == raw


@pytest.mark.asyncio
async def test_parse_image_event_returns_empty_text() -> None:
    raw = _feishu_text_event()
    raw["event"]["message"]["message_type"] = "image"
    raw["event"]["message"]["content"] = '{"image_key":"img_xxx"}'
    channel = _channel()
    envelope = await FeishuAdapter().parse_inbound(raw=raw, channel=channel)
    assert envelope.text == ""
    assert envelope.attachments == []  # M1: attachments deferred


@pytest.mark.asyncio
async def test_parse_malformed_content_does_not_crash() -> None:
    """Bad content JSON -> empty text rather than raising."""
    raw = _feishu_text_event()
    raw["event"]["message"]["content"] = "{not-json"
    channel = _channel()
    envelope = await FeishuAdapter().parse_inbound(raw=raw, channel=channel)
    assert envelope.text == ""


@pytest.mark.asyncio
async def test_send_outbound_delegates_to_client(monkeypatch) -> None:
    """FeishuAdapter.send_outbound should call the API client with the right args."""
    FeishuAdapter.reset_client()  # fresh instance

    captured: dict = {}

    async def fake_send(*, app_id, app_secret, receive_id, text):
        captured["app_id"] = app_id
        captured["app_secret"] = app_secret
        captured["receive_id"] = receive_id
        captured["text"] = text
        return {"code": 0, "msg": "ok", "data": {"message_id": "om_xyz"}}

    monkeypatch.setattr(FeishuAdapter._client, "send_text_message", fake_send)
    channel = _channel()
    envelope = MessageEnvelope(
        envelope_id=new_id(),
        tenant_id=channel.tenant_id,
        channel_type=ChannelType.FEISHU,
        channel_id=channel.id,
        external_conversation_id="oc_x",
        external_user_id="ou_user_99",
        external_message_id="om_in_1",
        text="reply text",
        attachments=[],
        raw={},
        received_at=datetime.now(UTC),
    )
    await FeishuAdapter().send_outbound(envelope=envelope, channel=channel)
    assert captured == {
        "app_id": "cli_test_app",
        "app_secret": "secret",
        "receive_id": "ou_user_99",
        "text": "reply text",
    }
    FeishuAdapter.reset_client()  # cleanup


def test_adapter_channel_type() -> None:
    assert FeishuAdapter().channel_type is ChannelType.FEISHU


@pytest.mark.asyncio
async def test_parse_minimally_empty_raw_does_not_crash() -> None:
    """An empty raw={} should not crash; fields default to empty strings."""
    channel = _channel()
    envelope = await FeishuAdapter().parse_inbound(raw={}, channel=channel)
    assert envelope.external_conversation_id == ""
    assert envelope.external_user_id == ""
    assert envelope.external_message_id == ""
    assert envelope.text == ""


@pytest.mark.asyncio
async def test_send_outbound_malformed_credentials_raises_feishu_api_error(monkeypatch) -> None:
    """Invalid credentials JSON should raise FeishuAPIError, not JSONDecodeError."""
    FeishuAdapter.reset_client()
    monkeypatch.setattr(FeishuAdapter._client, "send_text_message", lambda **kw: None)

    channel = _channel()
    channel.credentials_encrypted = "{not-valid-json"
    envelope = MessageEnvelope(
        envelope_id=new_id(),
        tenant_id=channel.tenant_id,
        channel_type=ChannelType.FEISHU,
        channel_id=channel.id,
        external_conversation_id="oc_x",
        external_user_id="ou_user",
        external_message_id="om_in",
        text="hi",
        attachments=[],
        raw={},
        received_at=datetime.now(UTC),
    )
    with pytest.raises(FeishuAPIError, match="credentials_encrypted"):
        await FeishuAdapter().send_outbound(envelope=envelope, channel=channel)
    FeishuAdapter.reset_client()


@pytest.mark.asyncio
async def test_send_outbound_missing_app_id_raises_feishu_api_error(monkeypatch) -> None:
    """Missing app_id should raise FeishuAPIError, not KeyError."""
    FeishuAdapter.reset_client()
    monkeypatch.setattr(FeishuAdapter._client, "send_text_message", lambda **kw: None)

    channel = _channel()
    channel.credentials_encrypted = '{"app_secret": "x"}'  # no app_id
    envelope = MessageEnvelope(
        envelope_id=new_id(),
        tenant_id=channel.tenant_id,
        channel_type=ChannelType.FEISHU,
        channel_id=channel.id,
        external_conversation_id="oc_x",
        external_user_id="ou_user",
        external_message_id="om_in",
        text="hi",
        attachments=[],
        raw={},
        received_at=datetime.now(UTC),
    )
    with pytest.raises(FeishuAPIError, match="missing credential field"):
        await FeishuAdapter().send_outbound(envelope=envelope, channel=channel)
    FeishuAdapter.reset_client()
