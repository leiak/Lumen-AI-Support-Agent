"""End-to-end: Feishu webhook -> adapter -> ConversationService -> DB.

Verifies that a verified Feishu webhook payload results in a new
Conversation + Message row in the database. Uses live DB; integration-only.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from channel.enums import ChannelType
from channel.feishu.signature import sign_feishu_payload
from channel.feishu.webhook import M1_STUB_ENCRYPT_KEY
from channel.feishu.webhook import router as feishu_webhook_router
from channel.repository import ChannelRepository
from conversation.enums import ConversationStatus, MessageRole
from conversation.repository import ConversationRepository, MessageRepository
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository


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


def _text_event(*, chat_id: str, open_id: str, message_id: str, text: str, app_id: str) -> bytes:
    return json.dumps({
        "schema": "2.0",
        "header": {
            "event_type": "im.message.receive_v1",
            "app_id": app_id,
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
    return sign_feishu_payload(
        timestamp=timestamp, nonce=nonce, encrypt_key=M1_STUB_ENCRYPT_KEY, body=body
    )


@pytest.mark.integration
async def test_feishu_webhook_creates_conversation_and_message(feishu_app: FastAPI) -> None:
    """End-to-end: signed Feishu event -> 200 ack + new Conversation + new Message in DB."""
    # Seed tenant + FEISHU channel whose credentials.app_id matches the URL.
    tenant_repo = TenantRepository()
    tenant = await tenant_repo.create(name="Pipeline Tenant", plan=TenantPlan.FREE)
    channel_repo = ChannelRepository()
    app_id = "cli_pipeline"
    channel = await channel_repo.create(
        tenant_id=tenant.id,
        type=ChannelType.FEISHU,
        name="Pipeline Feishu",
        credentials_encrypted=json.dumps({"app_id": app_id, "app_secret": "secret"}),
    )

    try:
        body = _text_event(
            chat_id="oc_pipeline_1",
            open_id="ou_user_pipeline",
            message_id="om_pipeline_1",
            text="hello pipeline",
            app_id=app_id,
        )
        ts = str(int(time.time()))
        sig = _sign(body, ts)
        async with AsyncClient(
            transport=ASGITransport(app=feishu_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/channel/feishu/webhook/{app_id}",
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

        # Verify DB state: one OPEN conversation for the (channel, customer) pair,
        # and one CUSTOMER message on it.
        conv_repo = ConversationRepository()
        conv = await conv_repo.find_open_by_channel_customer(
            channel_id=channel.id, customer_external_id="ou_user_pipeline"
        )
        assert conv is not None
        assert conv.tenant_id == tenant.id
        assert conv.status == ConversationStatus.OPEN
        assert conv.ai_handling is True

        msg_repo = MessageRepository()
        msgs = await msg_repo.list_by_conversation(conversation_id=conv.id)
        # Task 5.3: with ai_handling=True the inbound pipeline also records
        # an AI auto-reply (real LLM in production, fallback in CI without
        # a live ANTHROPIC_API_KEY). The CUSTOMER row must still be first.
        roles = [m.role for m in msgs]
        assert MessageRole.CUSTOMER in roles
        customer_msgs = [m for m in msgs if m.role == MessageRole.CUSTOMER]
        assert len(customer_msgs) == 1
        assert customer_msgs[0].content_text == "hello pipeline"
    finally:
        # Cascade-delete via tenant cleanup.
        from core.database import get_session

        async with get_session() as session:
            t = await session.get(Tenant, tenant.id)
            if t:
                await session.delete(t)
                await session.commit()


@pytest.mark.integration
async def test_feishu_webhook_repeated_message_reuses_conversation(
    feishu_app: FastAPI,
) -> None:
    """Two messages from the same customer produce ONE conversation with TWO messages."""
    tenant_repo = TenantRepository()
    tenant = await tenant_repo.create(name="Pipeline Tenant 2", plan=TenantPlan.FREE)
    channel_repo = ChannelRepository()
    app_id = "cli_pipeline_2"
    channel = await channel_repo.create(
        tenant_id=tenant.id,
        type=ChannelType.FEISHU,
        name="Pipeline Feishu 2",
        credentials_encrypted=json.dumps({"app_id": app_id}),
    )

    async def _post(message_id: str, text: str) -> None:
        body = _text_event(
            chat_id="oc_reuse",
            open_id="ou_reuse",
            message_id=message_id,
            text=text,
            app_id=app_id,
        )
        ts = str(int(time.time()))
        sig = _sign(body, ts)
        async with AsyncClient(
            transport=ASGITransport(app=feishu_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/channel/feishu/webhook/{app_id}",
                content=body,
                headers={
                    "X-Lark-Request-Timestamp": ts,
                    "X-Lark-Request-Nonce": "nonce1",
                    "X-Lark-Signature": sig,
                    "Content-Type": "application/json",
                },
            )
        assert resp.status_code == 200

    try:
        await _post("om_reuse_1", "first")
        await _post("om_reuse_2", "second")

        conv_repo = ConversationRepository()
        conv = await conv_repo.find_open_by_channel_customer(
            channel_id=channel.id, customer_external_id="ou_reuse"
        )
        assert conv is not None

        msg_repo = MessageRepository()
        msgs = await msg_repo.list_by_conversation(conversation_id=conv.id)
        # Task 5.3: each inbound also triggers an AI auto-reply, so we see
        # 2 customer messages + 2 AI rows (fallback or real, depending on
        # whether ANTHROPIC_API_KEY is valid in this test env).
        customer_msgs = [m for m in msgs if m.role == MessageRole.CUSTOMER]
        assert len(customer_msgs) == 2
        texts = sorted(m.content_text for m in customer_msgs)
        assert texts == ["first", "second"]
    finally:
        from core.database import get_session

        async with get_session() as session:
            t = await session.get(Tenant, tenant.id)
            if t:
                await session.delete(t)
                await session.commit()


@pytest.mark.integration
async def test_feishu_webhook_unknown_app_id_returns_404(feishu_app: FastAPI) -> None:
    """Webhook for an unknown app_id returns 404 (no matching channel)."""
    body = _text_event(
        chat_id="oc_404",
        open_id="ou_404",
        message_id="om_404",
        text="hi",
        app_id="cli_does_not_exist",
    )
    ts = str(int(time.time()))
    sig = _sign(body, ts)
    async with AsyncClient(
        transport=ASGITransport(app=feishu_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/channel/feishu/webhook/cli_does_not_exist",
            content=body,
            headers={
                "X-Lark-Request-Timestamp": ts,
                "X-Lark-Request-Nonce": "nonce1",
                "X-Lark-Signature": sig,
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 404
