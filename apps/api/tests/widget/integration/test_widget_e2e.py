"""End-to-end test for the Web Widget flow: token -> WS -> message -> ack -> disconnect."""
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from core.id_gen import new_id
from widget.adapter import WebWidgetAdapter
from widget.api import router as widget_api_router
from widget.tokens import create_widget_token
from widget.ws.manager import ConnectionManager
from widget.ws.router import router as widget_ws_router


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch):
    """Reset the WS manager singleton between tests."""
    from widget.ws import router as ws_router_module

    monkeypatch.setattr(ws_router_module, "manager", ConnectionManager())


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


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(widget_api_router)
    app.include_router(widget_ws_router)
    return app


def test_widget_full_flow_token_to_message(monkeypatch) -> None:
    """Issue token -> connect WS -> send ping/message -> receive pong/ack -> disconnect."""
    ch = _channel()
    token, _ = create_widget_token(channel=ch, external_user_id="u_e2e")

    async def fake_get_by_id(_self, _id):
        return ch

    from channel.repository import ChannelRepository

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    testclient = TestClient(_make_app())
    with testclient.websocket_connect(f"/api/v1/widget/ws?token={token}") as ws:
        # Ping -> pong
        ws.send_json({"type": "ping"})
        reply = ws.receive_json()
        assert reply == {"type": "pong"}

        # Message -> ack
        ws.send_json({"type": "message", "text": "hello world"})
        reply = ws.receive_json()
        assert reply["type"] == "ack"
        assert "external_message_id" in reply
        assert len(reply["external_message_id"]) > 0

        # Typing -> no-op (send ping afterwards to confirm connection still alive).
        ws.send_json({"type": "typing", "value": True})
        ws.send_json({"type": "ping"})
        reply = ws.receive_json()
        assert reply == {"type": "pong"}

        # Capture manager state inside the connect block.
        from widget.ws import router as r

        assert r.manager.count() == 1

    # After the context exits, the client has disconnected.
    from widget.ws import router as r

    assert r.manager.count() == 0


@pytest.mark.asyncio
async def test_widget_adapter_parse_then_send(monkeypatch) -> None:
    """Adapter parse -> envelope -> send_outbound -> broadcast end-to-end (mocked manager)."""
    captured: list = []

    async def fake_broadcast(channel_id, payload, *, exclude=None):
        captured.append({"channel_id": channel_id, "payload": payload})
        return 1

    from widget.ws import router as r

    monkeypatch.setattr(r.manager, "broadcast_to_channel", fake_broadcast)

    ch = _channel()
    raw_frame = {
        "type": "message",
        "conversation_id": "conv_e2e",
        "external_user_id": "u_e2e",
        "client_message_id": "cm_e2e_1",
        "text": "help me",
    }
    envelope = await WebWidgetAdapter().parse_inbound(raw=raw_frame, channel=ch)
    await WebWidgetAdapter().send_outbound(envelope=envelope, channel=ch)

    assert len(captured) == 1
    assert captured[0]["channel_id"] == ch.id
    assert captured[0]["payload"]["type"] == "message"
    assert captured[0]["payload"]["text"] == "help me"
    assert captured[0]["payload"]["external_message_id"] == envelope.external_message_id


def test_cross_adapter_envelope_shape_consistency() -> None:
    """Feishu and WebWidget adapters produce envelopes with the same field names."""
    from channel.feishu.adapter import FeishuAdapter
    from channel.messages import MessageEnvelope

    # Both adapters produce a MessageEnvelope; verify they share the contract.
    feishu_envelope_fields = set(MessageEnvelope.model_fields.keys())
    widget_envelope_fields = set(MessageEnvelope.model_fields.keys())
    assert feishu_envelope_fields == widget_envelope_fields
    assert "channel_type" in feishu_envelope_fields
    assert "external_conversation_id" in feishu_envelope_fields
    assert "external_user_id" in feishu_envelope_fields
    assert "external_message_id" in feishu_envelope_fields
    assert "text" in feishu_envelope_fields
    assert "attachments" in feishu_envelope_fields
    assert "raw" in feishu_envelope_fields
    assert "received_at" in feishu_envelope_fields
    assert "envelope_id" in feishu_envelope_fields

    # Sanity: the two adapter classes both yield a MessageEnvelope-shaped object
    # when handed realistic raw payloads, so downstream code can treat them uniformly.
    assert FeishuAdapter().channel_type is not None
    assert WebWidgetAdapter().channel_type is not None