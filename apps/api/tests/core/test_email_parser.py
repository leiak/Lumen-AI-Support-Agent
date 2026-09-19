"""Tests for SES inbound payload parser (M2.B / Stage 16)."""

from core.email_parser import ParsedEmail, parse_ses_inbound


def test_parse_simple_ses_payload():
    payload = b"""{
        "spamVerdict": {"status": "PASS"},
        "virusVerdict": {"status": "PASS"},
        "spfVerdict": {"status": "PASS"},
        "dkimVerdict": {"status": "PASS"},
        "commonHeaders": {
            "from": ["Alice <alice@example.com>"],
            "to": ["support@demo.test"],
            "subject": "How do I reset password?",
            "messageId": "<msg-001@example.com>",
            "inReplyTo": null,
            "references": null
        },
        "content": "I forgot my password, how do I reset it?"
    }"""
    result = parse_ses_inbound(payload)
    assert isinstance(result, ParsedEmail)
    assert result.from_address == "alice@example.com"
    assert result.to_address == "support@demo.test"
    assert result.subject == "How do I reset password?"
    assert result.body_text == "I forgot my password, how do I reset it?"
    assert result.message_id == "<msg-001@example.com>"
    assert result.in_reply_to is None


def test_parse_with_in_reply_to():
    payload = b"""{
        "commonHeaders": {
            "from": ["Bob <bob@example.com>"],
            "to": ["support@demo.test"],
            "subject": "Re: Login issue",
            "messageId": "<msg-002@example.com>",
            "inReplyTo": "<msg-001@example.com>",
            "references": ["<msg-001@example.com>"]
        },
        "content": "Still can't log in"
    }"""
    result = parse_ses_inbound(payload)
    assert result.in_reply_to == "<msg-001@example.com>"
    assert result.references == ["<msg-001@example.com>"]
    assert result.thread_id == "<msg-001@example.com>"  # References[0] is thread leader


def test_parse_malformed_payload_returns_none():
    assert parse_ses_inbound(b"not json") is None
    assert parse_ses_inbound(b"{}") is None  # Missing required fields
    assert parse_ses_inbound(b"") is None


def test_parse_extracts_address_from_name_angle_brackets():
    payload = b"""{
        "commonHeaders": {
            "from": ["Alice Smith <alice@example.com>"],
            "to": ["Support <support@demo.test>"],
            "subject": "Test",
            "messageId": "<m@x>"
        },
        "content": "hi"
    }"""
    result = parse_ses_inbound(payload)
    assert result.from_address == "alice@example.com"
    assert result.to_address == "support@demo.test"