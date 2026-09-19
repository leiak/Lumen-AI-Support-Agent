"""Integration tests for SES inbound webhook (Postgres + LLM mocked)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


@pytest.mark.integration
@pytest.mark.asyncio
async def test_email_inbound_creates_conversation_and_records_message(
    async_client: AsyncClient, db_session, sample_tenant
):
    payload = {
        "commonHeaders": {
            "from": ["Alice <alice@example.com>"],
            "to": ["support@demo.test"],
            "subject": "How do I reset password?",
            "messageId": "<msg-test-001@example.com>",
        },
        "content": "I forgot my password",
        "spamVerdict": {"status": "PASS"},
    }

    # Mock the LLM so the AI auto-reply doesn't actually call Anthropic
    fake_response = MagicMock()
    fake_response.content_text = "To reset your password, click here."
    fake_response.role = "ai"

    with patch(
        "agent.simple_responder.SimpleResponder.respond",
        new=AsyncMock(return_value=fake_response),
    ):
        resp = await async_client.post(
            "/api/v1/email/inbound",
            content=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Tenant-ID": sample_tenant.id,
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["conversation_id"]
    assert body["message_id"]
    assert body.get("ai_message_id")  # auto-reply generated


@pytest.mark.integration
@pytest.mark.asyncio
async def test_email_inbound_threads_on_references(
    async_client, db_session, sample_tenant
):
    """Same thread_id → same conversation."""
    payload1 = {
        "commonHeaders": {
            "from": ["Bob <bob@example.com>"],
            "to": ["support@demo.test"],
            "subject": "Login broken",
            "messageId": "<msg-001@example.com>",
        },
        "content": "can't log in",
    }
    resp1 = await async_client.post(
        "/api/v1/email/inbound",
        content=json.dumps(payload1).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Tenant-ID": sample_tenant.id,
        },
    )
    assert resp1.status_code == 200
    conv_id_1 = resp1.json()["conversation_id"]
    assert conv_id_1

    payload2 = {
        "commonHeaders": {
            "from": ["Bob <bob@example.com>"],
            "to": ["support@demo.test"],
            "subject": "Re: Login broken",
            "messageId": "<msg-002@example.com>",
            "inReplyTo": "<msg-001@example.com>",
            "references": ["<msg-001@example.com>"],
        },
        "content": "still broken",
    }
    resp2 = await async_client.post(
        "/api/v1/email/inbound",
        content=json.dumps(payload2).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Tenant-ID": sample_tenant.id,
        },
    )
    assert resp2.status_code == 200
    conv_id_2 = resp2.json()["conversation_id"]

    assert conv_id_1 == conv_id_2  # Same thread → same conversation


@pytest.mark.integration
@pytest.mark.asyncio
async def test_email_inbound_dedupes_on_message_id(
    async_client, db_session, sample_tenant
):
    """Duplicate webhook (SES retry) → no duplicate conversation."""
    payload = {
        "commonHeaders": {
            "from": ["Carl <carl@example.com>"],
            "to": ["support@demo.test"],
            "subject": "Question",
            "messageId": "<dup-msg-001@example.com>",
        },
        "content": "first send",
    }
    body_bytes = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Tenant-ID": sample_tenant.id,
    }

    resp1 = await async_client.post(
        "/api/v1/email/inbound", content=body_bytes, headers=headers,
    )
    resp2 = await async_client.post(
        "/api/v1/email/inbound", content=body_bytes, headers=headers,
    )

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json()["conversation_id"] == resp2.json()["conversation_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_email_inbound_returns_200_on_malformed(async_client):
    """SES retries on 5xx — malformed payload must return 200 to stop retry."""
    resp = await async_client.post(
        "/api/v1/email/inbound",
        content=b"not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped"
