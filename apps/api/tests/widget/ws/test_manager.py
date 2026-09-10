"""Tests for the WebSocket connection manager (in-process)."""
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI

from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from core.id_gen import new_id
from widget.ws.manager import ConnectionManager
from widget.ws.router import router as ws_router


class FakeWS:
    """Lightweight WebSocket double for manager tests."""

    def __init__(self) -> None:
        self.sent: list = []

    async def send_json(self, payload) -> None:
        self.sent.append(payload)


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


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ws_router)
    return app


@pytest.fixture
def manager() -> ConnectionManager:
    return ConnectionManager()


# --- ConnectionManager unit tests ---

@pytest.mark.asyncio
async def test_connect_registers_connection(manager: ConnectionManager) -> None:
    fake_ws = FakeWS()
    cid = await manager.connect(
        websocket=fake_ws,
        channel_id="ch1",
        tenant_id="t1",
        external_user_id="u1",
    )
    state = manager.get_state(cid)
    assert state is not None
    assert state.channel_id == "ch1"
    assert state.tenant_id == "t1"
    assert state.external_user_id == "u1"


@pytest.mark.asyncio
async def test_disconnect_removes_connection(manager: ConnectionManager) -> None:
    fake_ws = FakeWS()
    cid = await manager.connect(
        websocket=fake_ws,
        channel_id="ch1",
        tenant_id="t1",
        external_user_id="u1",
    )
    await manager.disconnect(cid)
    assert manager.get_state(cid) is None
    assert manager.list_connections("ch1") == []


@pytest.mark.asyncio
async def test_list_connections_filters_by_channel(manager: ConnectionManager) -> None:
    fake_ws = FakeWS()
    await manager.connect(
        websocket=fake_ws, channel_id="ch_a", tenant_id="t1", external_user_id="u1"
    )
    await manager.connect(
        websocket=fake_ws, channel_id="ch_a", tenant_id="t1", external_user_id="u2"
    )
    await manager.connect(
        websocket=fake_ws, channel_id="ch_b", tenant_id="t1", external_user_id="u3"
    )
    assert len(manager.list_connections("ch_a")) == 2
    assert len(manager.list_connections("ch_b")) == 1
    assert manager.count() == 3


@pytest.mark.asyncio
async def test_send_to_connection_invokes_send_json(manager: ConnectionManager) -> None:
    """send_to_connection should call ws.send_json() with the payload."""
    fake_ws = FakeWS()
    cid = await manager.connect(
        websocket=fake_ws, channel_id="ch1", tenant_id="t1", external_user_id="u1"
    )
    await manager.send_to_connection(cid, {"type": "ping"})
    assert fake_ws.sent == [{"type": "ping"}]


@pytest.mark.asyncio
async def test_send_to_unknown_connection_is_noop(manager: ConnectionManager) -> None:
    """Sending to a nonexistent connection should not raise."""
    await manager.send_to_connection("does_not_exist", {"type": "ping"})


@pytest.mark.asyncio
async def test_broadcast_to_channel_sends_to_all(manager: ConnectionManager) -> None:
    fake_a = FakeWS()
    fake_b = FakeWS()
    fake_c = FakeWS()
    await manager.connect(
        websocket=fake_a, channel_id="ch_a", tenant_id="t1", external_user_id="u1"
    )
    cid_b = await manager.connect(
        websocket=fake_b, channel_id="ch_a", tenant_id="t1", external_user_id="u2"
    )
    await manager.connect(
        websocket=fake_c, channel_id="ch_b", tenant_id="t1", external_user_id="u3"
    )
    await manager.broadcast_to_channel("ch_a", {"type": "evt"}, exclude=cid_b)
    assert fake_a.sent == [{"type": "evt"}]
    assert fake_b.sent == []  # excluded
    assert fake_c.sent == []  # wrong channel
