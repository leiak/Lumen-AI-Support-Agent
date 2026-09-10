"""End-to-end test for the Feishu inbound flow: webhook -> adapter -> envelope."""
import json
import time
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from channel.enums import ChannelStatus, ChannelType
from channel.feishu.adapter import FeishuAdapter
from channel.feishu.signature import sign_feishu_payload
from channel.feishu.webhook import M1_STUB_ENCRYPT_KEY
from channel.feishu.webhook import router as feishu_webhook_router
from channel.messages import MessageEnvelope
from channel.models import Channel
from core.id_gen import new_id


@pytest.fixture(autouse=True)
def _reset_db_singletons():
    """Reset async engine/sessionmaker between tests to avoid cross-loop contamination."""
    from core.database import reset_engine, reset_sessionmaker

    yield
    reset_engine()
    reset_sessionmaker()


@pytest.fixture
def feishu_app() -> FastAPI:
    app = FastAPI()
    app.include_router(feishu_webhook_router)
    return app


def _text_event(*, chat_id: str, open_id: str, message_id: str, text: str) -> bytes:
    return json.dumps({
        "schema": "2.0",
        "header": {
            "event_type": "im.message.receive_v1",
            "app_id": "cli_test_app",
            "tenant_key": "t1",
            "event_id": "ev_1",
            "create_time": "1700000000000",
        },
        "event": {
            "sender": {"sender_id": {"open_id": open_id}},
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }).encode("utf-8")


def _sign(body: bytes, timestamp: str, nonce: str = "nonce1") -> str:
    """Sign a body with the M1 stub encrypt_key the webhook expects."""
    return sign_feishu_payload(
        timestamp=timestamp, nonce=nonce, encrypt_key=M1_STUB_ENCRYPT_KEY, body=body
    )


@pytest.mark.asyncio
async def test_feishu_inbound_e2e_signed_payload(feishu_app: FastAPI) -> None:
    """Webhook accepts a signed Feishu text event and returns 200."""
    body = _text_event(chat_id="oc_1", open_id="ou_user_1", message_id="om_1", text="hello")
    ts = str(int(time.time()))
    sig = _sign(body, ts)
    async with AsyncClient(
        transport=ASGITransport(app=feishu_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/channel/feishu/webhook/cli_test_app",
            content=body,
            headers={
                "X-Lark-Request-Timestamp": ts,
                "X-Lark-Request-Nonce": "nonce1",
                "X-Lark-Signature": sig,
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_feishu_adapter_produces_envelope() -> None:
    """FeishuAdapter.parse_inbound produces a well-formed MessageEnvelope."""
    raw = json.loads(
        _text_event(chat_id="oc_xyz", open_id="ou_abc", message_id="om_xyz", text="hi there")
    )
    channel = Channel(
        id=new_id(),
        tenant_id=new_id(),
        type=ChannelType.FEISHU,
        name="x",
        status=ChannelStatus.ACTIVE,
        credentials_encrypted="{}",
        created_at=datetime.now(UTC),
    )
    envelope = await FeishuAdapter().parse_inbound(raw=raw, channel=channel)
    assert isinstance(envelope, MessageEnvelope)
    assert envelope.channel_type == ChannelType.FEISHU
    assert envelope.external_conversation_id == "oc_xyz"
    assert envelope.external_user_id == "ou_abc"
    assert envelope.external_message_id == "om_xyz"
    assert envelope.text == "hi there"
    assert envelope.attachments == []
    assert envelope.received_at is not None


@pytest.mark.asyncio
async def test_feishu_outbound_roundtrip(httpx_mock) -> None:
    """FeishuAdapter.send_outbound uses the FeishuOpenAPIClient + tenant_access_token."""
    # Reset the singleton so any cached tokens from previous tests don't leak in.
    FeishuAdapter.reset_client()

    httpx_mock.add_response(
        url="https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"code": 0, "msg": "ok", "tenant_access_token": "t-e2e", "expire": 7200},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
        json={"code": 0, "msg": "ok", "data": {"message_id": "om_out_xyz"}},
    )

    channel = Channel(
        id=new_id(),
        tenant_id=new_id(),
        type=ChannelType.FEISHU,
        name="x",
        status=ChannelStatus.ACTIVE,
        credentials_encrypted=json.dumps({
            "app_id": "cli_e2e",
            "app_secret": "secret_e2e",
            "verification_token": "token",
            "encrypt_key": "",
        }),
        created_at=datetime.now(UTC),
    )
    envelope = MessageEnvelope(
        envelope_id=new_id(),
        tenant_id=channel.tenant_id,
        channel_type=ChannelType.FEISHU,
        channel_id=channel.id,
        external_conversation_id="oc_1",
        external_user_id="ou_user_1",
        external_message_id="om_1",
        text="reply text",
        attachments=[],
        raw={},
        received_at=datetime.now(UTC),
    )
    await FeishuAdapter().send_outbound(envelope=envelope, channel=channel)
    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    # First request: token; second: send
    assert "tenant_access_token" in str(requests[0].url)
    assert "im/v1/messages" in str(requests[1].url)

    # Cleanup
    FeishuAdapter.reset_client()