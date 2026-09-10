"""Tests for the Feishu webhook HTTP handler."""
import json
import time
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from channel.enums import ChannelStatus, ChannelType
from channel.feishu.signature import sign_feishu_payload
from channel.feishu.webhook import M1_STUB_ENCRYPT_KEY
from channel.feishu.webhook import router as webhook_router
from core.id_gen import new_id

TEST_ENCRYPT_KEY = M1_STUB_ENCRYPT_KEY


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(webhook_router)
    return app


def _sign(body: bytes, *, timestamp: str, nonce: str = "nonce1") -> str:
    return sign_feishu_payload(
        timestamp=timestamp,
        nonce=nonce,
        encrypt_key=TEST_ENCRYPT_KEY,
        body=body,
    )


def _text_event_json() -> bytes:
    return json.dumps(
        {
            "schema": "2.0",
            "header": {
                "event_type": "im.message.receive_v1",
                "app_id": "cli_test_app",
                "tenant_key": "t1",
                "event_id": "ev_1",
                "create_time": "1700000000000",
            },
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user_1"}},
                "message": {
                    "message_id": "om_msg_1",
                    "chat_id": "oc_chat_1",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": '{"text":"hi from feishu"}',
                },
            },
        }
    ).encode("utf-8")


def _fake_channel_for_app_id(app_id: str):
    """Build a fake Channel row whose app_id matches the URL parameter."""
    from channel.models import Channel

    return Channel(
        id=new_id(),
        tenant_id=new_id(),
        type=ChannelType.FEISHU,
        name="x",
        status=ChannelStatus.ACTIVE,
        credentials_encrypted=json.dumps({"app_id": app_id, "app_secret": "s"}),
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_url_verification_returns_challenge() -> None:
    """Feishu's URL verification handshake."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/channel/feishu/webhook/cli_test_app/verify",
            json={"challenge": "test_challenge_string"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "test_challenge_string"}


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_signature() -> None:
    app = _build_app()
    body = _text_event_json()
    timestamp = str(int(time.time()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/channel/feishu/webhook/cli_test_app",
            content=body,
            headers={
                "X-Lark-Request-Timestamp": timestamp,
                "X-Lark-Request-Nonce": "nonce1",
                "X-Lark-Signature": "0" * 64,  # wrong sig
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_webhook_accepts_valid_signature(monkeypatch) -> None:
    """A correctly signed body should return 200 + ack.

    Task 5.2 wires persistence: we now look up the channel by app_id,
    parse an envelope, and call process_inbound_envelope. Stub both so
    no DB is needed.
    """
    from channel.repository import ChannelRepository

    fake_ch = _fake_channel_for_app_id("cli_test_app")

    async def fake_get_by_app_id(self, app_id):  # type: ignore[no-untyped-def]
        return fake_ch if app_id == "cli_test_app" else None

    monkeypatch.setattr(ChannelRepository, "get_by_app_id", fake_get_by_app_id)

    captured: dict = {}

    async def fake_process(env):  # type: ignore[no-untyped-def]
        captured["envelope"] = env

    monkeypatch.setattr("channel.feishu.webhook.process_inbound_envelope", fake_process)

    app = _build_app()
    body = _text_event_json()
    timestamp = str(int(time.time()))
    sig = _sign(body, timestamp=timestamp)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/channel/feishu/webhook/cli_test_app",
            content=body,
            headers={
                "X-Lark-Request-Timestamp": timestamp,
                "X-Lark-Request-Nonce": "nonce1",
                "X-Lark-Signature": sig,
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # And the inbound processor was called.
    assert "envelope" in captured
    assert captured["envelope"].channel_type == ChannelType.FEISHU
    assert captured["envelope"].text == "hi from feishu"


@pytest.mark.asyncio
async def test_webhook_returns_404_for_unknown_app_id(monkeypatch) -> None:
    """An app_id with no matching channel returns 404."""
    from channel.repository import ChannelRepository

    async def fake_get_by_app_id(self, _app_id):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(ChannelRepository, "get_by_app_id", fake_get_by_app_id)

    app = _build_app()
    body = _text_event_json()
    timestamp = str(int(time.time()))
    sig = _sign(body, timestamp=timestamp)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/channel/feishu/webhook/cli_test_app",
            content=body,
            headers={
                "X-Lark-Request-Timestamp": timestamp,
                "X-Lark-Request-Nonce": "nonce1",
                "X-Lark-Signature": sig,
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 404
    assert "unknown app_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_still_acks_when_persistence_fails(monkeypatch) -> None:
    """A persistence error inside the real processor must NOT propagate — webhook ACKs 200.

    Verifies the end-to-end webhook-to-processor contract: the real
    ``process_inbound_envelope`` swallows DB errors.
    """
    from unittest.mock import AsyncMock, patch

    from channel.repository import ChannelRepository

    fake_ch = _fake_channel_for_app_id("cli_test_app")

    async def fake_get_by_app_id(self, _app_id):  # type: ignore[no-untyped-def]
        return fake_ch

    monkeypatch.setattr(ChannelRepository, "get_by_app_id", fake_get_by_app_id)

    with patch("channel.inbound.ConversationService") as mock_svc_cls:
        mock_service = mock_svc_cls.return_value
        mock_service.find_or_create_for_inbound = AsyncMock(
            side_effect=RuntimeError("db down")
        )

        app = _build_app()
        body = _text_event_json()
        timestamp = str(int(time.time()))
        sig = _sign(body, timestamp=timestamp)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/channel/feishu/webhook/cli_test_app",
                content=body,
                headers={
                    "X-Lark-Request-Timestamp": timestamp,
                    "X-Lark-Request-Nonce": "nonce1",
                    "X-Lark-Signature": sig,
                    "Content-Type": "application/json",
                },
            )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
