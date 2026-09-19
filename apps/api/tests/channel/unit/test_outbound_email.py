"""Tests for SES SendEmail outbound client (M2.B / Stage 16)."""
import json
from unittest.mock import MagicMock, patch

import pytest

from channel.outbound_email import EmailOutbound, EmailSendError


@pytest.fixture
def outbound():
    return EmailOutbound(
        region="us-east-1",
        from_address="support@demo.test",
        aws_access_key_id="AKIA-TEST",
        aws_secret_access_key="secret-test",  # noqa: S106 — test fixture stub
    )


@pytest.mark.asyncio
async def test_send_reply_signs_and_posts(outbound):
    """Verify SES SendEmail HTTP API call structure.

    Implementation uses SigV4 over raw bytes (content=), not httpx's
    json= kwarg, so we read the signed body from captured["content"]
    and json.loads() it ourselves to assert on the SES payload shape.
    """
    captured = {}
    async def fake_post(url, *, json=None, headers=None, content=None, **kwargs):
        captured["url"] = url
        captured["content"] = content
        captured["headers"] = headers
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"MessageId": "ses-001"}
        return resp

    with patch("httpx.AsyncClient.post", side_effect=fake_post):
        msg_id = await outbound.send_reply(
            tenant_id="t1",
            to_email="alice@example.com",
            subject="Re: Test",
            body_text="Hi Alice",
            in_reply_to="<msg-001@example.com>",
            references=["<msg-001@example.com>"],
        )

    assert msg_id == "ses-001"
    assert "email.us-east-1.amazonaws.com" in captured["url"]
    assert "v2/email/outbound-emails" in captured["url"]
    # SES SendEmail payload structure — parsed from signed content= bytes
    body = json.loads(captured["content"].decode("utf-8"))
    assert body["FromEmailAddress"] == "support@demo.test"
    assert body["Destination"]["ToAddresses"] == ["alice@example.com"]
    assert body["Content"]["Simple"]["Subject"]["Data"] == "Re: Test"
    assert body["Content"]["Simple"]["Body"]["Text"]["Data"] == "Hi Alice"
    # Headers include thread tracking
    headers_list = body["Content"]["Simple"]["Headers"]
    assert any(h["Name"].lower() == "in-reply-to" for h in headers_list)
    assert any(h["Name"].lower() == "references" for h in headers_list)


@pytest.mark.asyncio
async def test_send_reply_retries_on_5xx(outbound):
    """5xx -> retry up to 3 times, then raise EmailSendError."""
    call_count = 0
    async def fake_post(url, **kwargs):
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        resp.status_code = 500
        resp.text = "Internal Error"
        return resp

    with patch("httpx.AsyncClient.post", side_effect=fake_post):
        with pytest.raises(EmailSendError):
            await outbound.send_reply(
                tenant_id="t1", to_email="a@x.com",
                subject="s", body_text="b",
                in_reply_to=None, references=[],
            )
    assert call_count == 3  # 3 attempts before giving up


@pytest.mark.asyncio
async def test_send_reply_no_retry_on_4xx(outbound):
    """4xx -> raise immediately (don't retry; it's a permanent failure)."""
    call_count = 0
    async def fake_post(url, **kwargs):
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        resp.status_code = 400
        resp.text = "Bad Request"
        resp.json.return_value = {"error": "InvalidParameterValue"}
        return resp

    with patch("httpx.AsyncClient.post", side_effect=fake_post):
        with pytest.raises(EmailSendError):
            await outbound.send_reply(
                tenant_id="t1", to_email="a@x.com",
                subject="s", body_text="b",
                in_reply_to=None, references=[],
            )
    assert call_count == 1


@pytest.mark.asyncio
async def test_send_reply_returns_message_id(outbound):
    """Happy path returns SES MessageId."""
    async def fake_post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"MessageId": "ses-xyz-789"}
        return resp

    with patch("httpx.AsyncClient.post", side_effect=fake_post):
        msg_id = await outbound.send_reply(
            tenant_id="t1", to_email="a@x.com",
            subject="s", body_text="b",
            in_reply_to=None, references=[],
        )
    assert msg_id == "ses-xyz-789"


@pytest.mark.asyncio
async def test_send_reply_error_carries_status_code(outbound):
    """EmailSendError must expose status_code + error_type for downstream handling."""
    async def fake_post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 403
        resp.text = "AccessDenied"
        return resp

    with patch("httpx.AsyncClient.post", side_effect=fake_post):
        with pytest.raises(EmailSendError) as exc_info:
            await outbound.send_reply(
                tenant_id="t1", to_email="a@x.com",
                subject="s", body_text="b",
                in_reply_to=None, references=[],
            )
    err = exc_info.value
    assert err.status_code == 403
    assert err.error_type == "SES4xx"
    assert err.attempts == 1  # 4xx doesn't retry