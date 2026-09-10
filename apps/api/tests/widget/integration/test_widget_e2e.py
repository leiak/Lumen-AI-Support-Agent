"""End-to-end test for the Web Widget flow: token -> WS -> message -> ack -> disconnect."""
import json
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


@pytest.mark.asyncio
async def test_cross_adapter_envelope_consistency_real() -> None:
    """Both adapters must produce MessageEnvelope with consistent field types and shapes."""
    from channel.feishu.adapter import FeishuAdapter

    # Same channel record for both adapters (simulating a real DB lookup)
    shared_channel = Channel(
        id=new_id(),
        tenant_id=new_id(),
        type=ChannelType.WEB,
        name="x",
        status=ChannelStatus.ACTIVE,
        credentials_encrypted="{}",
        created_at=datetime.now(UTC),
    )

    feishu_raw = {
        "event": {
            "sender": {"sender_id": {"open_id": "ou_shared"}},
            "message": {
                "message_id": "om_shared",
                "chat_id": "oc_shared",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": "shared text"}),
            },
        }
    }
    widget_raw = {
        "type": "message",
        "conversation_id": "oc_shared",
        "external_user_id": "ou_shared",
        "client_message_id": "om_shared",
        "text": "shared text",
    }

    feishu_envelope = await FeishuAdapter().parse_inbound(raw=feishu_raw, channel=shared_channel)
    widget_envelope = await WebWidgetAdapter().parse_inbound(raw=widget_raw, channel=shared_channel)

    # Both must populate the same canonical fields with the same values
    for f in ("external_conversation_id", "external_user_id", "external_message_id", "text"):
        assert getattr(feishu_envelope, f) == getattr(widget_envelope, f), (
            f"{f}: feishu={getattr(feishu_envelope, f)!r} widget={getattr(widget_envelope, f)!r}"
        )
    # Both must have non-empty envelope_id, channel_type, attachments=[], and received_at
    assert isinstance(feishu_envelope.envelope_id, str) and len(feishu_envelope.envelope_id) > 0
    assert isinstance(widget_envelope.envelope_id, str) and len(widget_envelope.envelope_id) > 0
    assert feishu_envelope.attachments == []
    assert widget_envelope.attachments == []
    assert isinstance(feishu_envelope.received_at, datetime)
    assert isinstance(widget_envelope.received_at, datetime)


def test_widget_ws_rejects_garbage_token_integration(monkeypatch) -> None:
    """Integration-level: garbage token fails WS handshake."""
    from fastapi import WebSocketDisconnect

    from channel.repository import ChannelRepository

    async def fake_get_by_id(_self, _id):
        return None

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    testclient = TestClient(_make_app())
    with pytest.raises(WebSocketDisconnect):
        with testclient.websocket_connect("/api/v1/widget/ws?token=garbage.token.value"):
            pass


def test_widget_ws_rejects_disabled_channel_integration(monkeypatch) -> None:
    """Integration-level: token for a DISABLED channel fails WS handshake."""
    from fastapi import WebSocketDisconnect

    from channel.repository import ChannelRepository

    ch = Channel(
        id=new_id(),
        tenant_id=new_id(),
        type=ChannelType.WEB,
        name="x",
        status=ChannelStatus.DISABLED,
        credentials_encrypted="{}",
        created_at=datetime.now(UTC),
    )
    token, _ = create_widget_token(channel=ch, external_user_id="u1")

    async def fake_get_by_id(_self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    testclient = TestClient(_make_app())
    with pytest.raises(WebSocketDisconnect):
        with testclient.websocket_connect(f"/api/v1/widget/ws?token={token}"):
            pass


def test_widget_ws_rejects_cross_tenant_token_integration(monkeypatch) -> None:
    """Integration-level: token's tenant must match channel's tenant."""
    from fastapi import WebSocketDisconnect

    from channel.repository import ChannelRepository

    token_channel = Channel(
        id=new_id(),
        tenant_id="tenant_token",
        type=ChannelType.WEB,
        name="x",
        status=ChannelStatus.ACTIVE,
        credentials_encrypted="{}",
        created_at=datetime.now(UTC),
    )
    token, _ = create_widget_token(channel=token_channel, external_user_id="u1")

    async def fake_get_by_id(_self, _id):
        # Return a channel with a different tenant than the token's channel
        return Channel(
            id=token_channel.id,
            tenant_id="tenant_different",
            type=ChannelType.WEB,
            name="x",
            status=ChannelStatus.ACTIVE,
            credentials_encrypted="{}",
            created_at=datetime.now(UTC),
        )

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    testclient = TestClient(_make_app())
    with pytest.raises(WebSocketDisconnect):
        with testclient.websocket_connect(f"/api/v1/widget/ws?token={token}"):
            pass