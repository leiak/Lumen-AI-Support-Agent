"""Tests for widget WS inbound message persistence wiring."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from channel.repository import ChannelRepository
from core.id_gen import new_id
from widget.tokens import create_widget_token
from widget.ws.manager import ConnectionManager
from widget.ws.router import router as ws_router


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


@pytest.fixture
def fresh_manager(monkeypatch: pytest.MonkeyPatch) -> ConnectionManager:
    """Reset the module-level manager between tests."""
    from widget.ws import router as r

    new_mgr = ConnectionManager()
    monkeypatch.setattr(r, "manager", new_mgr)
    return new_mgr


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ws_router)
    return app


@pytest.mark.asyncio
async def test_widget_ws_inbound_calls_process_inbound_envelope(
    fresh_manager, monkeypatch
) -> None:
    """Widget WS message -> adapter -> process_inbound_envelope called."""
    ch = _channel()
    token, _ = create_widget_token(channel=ch, external_user_id="u1")

    async def fake_get_by_id(_self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    # Capture the envelope passed to process_inbound_envelope.
    captured: dict = {}

    async def fake_process(envelope):  # type: ignore[no-untyped-def]
        captured["envelope"] = envelope

    monkeypatch.setattr("widget.ws.router.process_inbound_envelope", fake_process)

    testclient = TestClient(_make_app())
    with testclient.websocket_connect(f"/api/v1/widget/ws?token={token}") as ws:
        ws.send_json({
            "type": "message",
            "text": "hello via ws",
            "conversation_id": "oc_ws_1",
            "external_user_id": "u1",
            "client_message_id": "cm_ws_1",
        })
        reply = ws.receive_json()
        assert reply["type"] == "ack"
        assert "external_message_id" in reply
        assert len(reply["external_message_id"]) > 0

    assert "envelope" in captured, "process_inbound_envelope was not called"
    env = captured["envelope"]
    assert env.text == "hello via ws"
    assert env.channel_type == ChannelType.WEB
    assert env.channel_id == ch.id
    assert env.tenant_id == ch.tenant_id
    assert env.external_user_id == "u1"
    assert env.external_conversation_id == "oc_ws_1"


@pytest.mark.asyncio
async def test_widget_ws_inbound_still_acks_when_persistence_fails(
    fresh_manager, monkeypatch
) -> None:
    """Persistence errors inside the real processor must NOT propagate — WS still ACKs.

    Verifies the end-to-end WS-to-processor contract: the real
    ``process_inbound_envelope`` swallows DB errors, so the WS handler
    always ACKs.
    """
    ch = _channel()
    token, _ = create_widget_token(channel=ch, external_user_id="u1")

    async def fake_get_by_id(_self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    # Make the real process_inbound_envelope hit a ConversationService that
    # raises — the processor's try/except should swallow it.
    with patch("channel.inbound.ConversationService") as mock_svc_cls:
        mock_service = mock_svc_cls.return_value
        mock_service.find_or_create_for_inbound = AsyncMock(
            side_effect=RuntimeError("db down")
        )

        testclient = TestClient(_make_app())
        with testclient.websocket_connect(f"/api/v1/widget/ws?token={token}") as ws:
            ws.send_json({"type": "message", "text": "will fail"})
            reply = ws.receive_json()
            assert reply["type"] == "ack"
