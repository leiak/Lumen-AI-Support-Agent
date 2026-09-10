"""Tests for the Feishu webhook HTTP handler."""
import json
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from channel.feishu.signature import sign_feishu_payload
from channel.feishu.webhook import router as webhook_router
from core.database import reset_engine, reset_sessionmaker

TEST_ENCRYPT_KEY = "test_encrypt_key_xxxxxxxxxxxxxxxxxxxxxxxxxx"


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


@pytest.fixture(autouse=True)
def _reset_db_singletons():
    """Each test gets a fresh engine — prevent cross-test event loop contamination."""
    yield
    reset_engine()
    reset_sessionmaker()


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
