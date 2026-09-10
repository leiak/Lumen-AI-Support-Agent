"""Tests for the WebSocket endpoint authentication and basic frame loop."""
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from auth.jwt import create_access_token
from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from channel.repository import ChannelRepository
from core.id_gen import new_id
from widget.tokens import create_widget_token
from widget.ws.manager import ConnectionManager
from widget.ws.router import router as ws_router


def _channel(*, status: ChannelStatus = ChannelStatus.ACTIVE) -> Channel:
    return Channel(
        id=new_id(),
        tenant_id=new_id(),
        type=ChannelType.WEB,
        name="x",
        status=status,
        credentials_encrypted="{}",
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def fresh_manager(monkeypatch) -> ConnectionManager:
    """Reset the module-level manager between tests via monkeypatch teardown."""
    from widget.ws import router as r

    new_mgr = ConnectionManager()
    monkeypatch.setattr(r, "manager", new_mgr)
    return new_mgr


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ws_router)
    return app


@pytest.mark.asyncio
async def test_ws_endpoint_rejects_invalid_token(fresh_manager, monkeypatch) -> None:
    """An invalid token must cause the server to close the connection."""
    testclient = TestClient(_make_app())
    with pytest.raises(WebSocketDisconnect):
        with testclient.websocket_connect(
            "/api/v1/widget/ws?token=garbage.token.value"
        ) as ws:
            ws.receive_text()


@pytest.mark.asyncio
async def test_ws_endpoint_rejects_admin_token(fresh_manager, monkeypatch) -> None:
    """Admin access tokens must not authenticate the WS endpoint."""
    admin_token = create_access_token(
        tenant_id="t1",
        user_id="u_admin",
        role="admin",
        extra={"typ": "access"},
    )
    testclient = TestClient(_make_app())
    with pytest.raises(WebSocketDisconnect):
        with testclient.websocket_connect(
            f"/api/v1/widget/ws?token={admin_token}"
        ) as ws:
            ws.receive_text()


@pytest.mark.asyncio
async def test_ws_endpoint_rejects_disabled_channel(fresh_manager, monkeypatch) -> None:
    ch = _channel(status=ChannelStatus.DISABLED)
    token, _ = create_widget_token(channel=ch, external_user_id="u1")

    async def fake_get_by_id(_self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    testclient = TestClient(_make_app())
    with pytest.raises(WebSocketDisconnect):
        with testclient.websocket_connect(
            f"/api/v1/widget/ws?token={token}"
        ) as ws:
            ws.receive_text()


@pytest.mark.asyncio
async def test_ws_endpoint_accepts_widget_token_and_pongs(
    fresh_manager, monkeypatch
) -> None:
    """A valid widget token connects; sending ping yields pong."""
    ch = _channel()
    token, _ = create_widget_token(channel=ch, external_user_id="u1")

    async def fake_get_by_id(_self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    testclient = TestClient(_make_app())
    with testclient.websocket_connect(f"/api/v1/widget/ws?token={token}") as ws:
        ws.send_json({"type": "ping"})
        reply = ws.receive_json()
        assert reply == {"type": "pong"}


@pytest.mark.asyncio
async def test_ws_endpoint_message_frame_yields_ack(
    fresh_manager, monkeypatch
) -> None:
    """Sending a 'message' frame yields an ack with external_message_id."""
    ch = _channel()
    token, _ = create_widget_token(channel=ch, external_user_id="u1")

    async def fake_get_by_id(_self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    testclient = TestClient(_make_app())
    with testclient.websocket_connect(f"/api/v1/widget/ws?token={token}") as ws:
        ws.send_json({"type": "message", "text": "hi"})
        reply = ws.receive_json()
        assert reply["type"] == "ack"
        assert "external_message_id" in reply
        assert len(reply["external_message_id"]) > 0


@pytest.mark.asyncio
async def test_ws_endpoint_typing_frame_noop(fresh_manager, monkeypatch) -> None:
    """A 'typing' frame should be silently accepted (no reply)."""
    ch = _channel()
    token, _ = create_widget_token(channel=ch, external_user_id="u1")

    async def fake_get_by_id(_self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    testclient = TestClient(_make_app())
    with testclient.websocket_connect(f"/api/v1/widget/ws?token={token}") as ws:
        ws.send_json({"type": "typing", "value": True})
        # No reply expected — type is no-op.
        # Send a ping to verify the connection is still alive.
        ws.send_json({"type": "ping"})
        reply = ws.receive_json()
        assert reply == {"type": "pong"}


@pytest.mark.asyncio
async def test_ws_endpoint_tenant_mismatch_rejected(fresh_manager, monkeypatch) -> None:
    """A widget token for tenant A must not connect to a channel owned by tenant B."""
    ch = _channel()
    # Make the token's tenant_id differ from the channel's tenant_id.
    token, _ = create_widget_token(channel=ch, external_user_id="u1")

    async def fake_get_by_id(_self, _id):
        # Return a channel with a different tenant than the token's.
        return Channel(
            id=ch.id,
            tenant_id="tenant_A",
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


@pytest.mark.asyncio
async def test_ws_endpoint_disconnect_clears_manager(fresh_manager, monkeypatch) -> None:
    """Client disconnect must remove the entry from the manager."""
    ch = _channel()
    token, _ = create_widget_token(channel=ch, external_user_id="u1")

    async def fake_get_by_id(_self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    testclient = TestClient(_make_app())
    with testclient.websocket_connect(f"/api/v1/widget/ws?token={token}") as ws:
        ws.send_json({"type": "ping"})
        ws.receive_json()
        assert fresh_manager.count() == 1
    # After the context exits, the client has disconnected.
    assert fresh_manager.count() == 0