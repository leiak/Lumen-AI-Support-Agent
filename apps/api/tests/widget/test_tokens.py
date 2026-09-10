"""Tests for widget token issuance service."""
from datetime import UTC, datetime

import pytest

from auth.jwt import TokenError, create_access_token, decode_token
from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from core.id_gen import new_id
from widget.tokens import (
    WIDGET_TOKEN_TTL_SECONDS,
    create_widget_token,
    decode_widget_token,
)


def _channel() -> Channel:
    return Channel(
        id=new_id(),
        tenant_id=new_id(),
        type=ChannelType.WEB,
        name="widget",
        status=ChannelStatus.ACTIVE,
        credentials_encrypted="{}",
        created_at=datetime.now(UTC),
    )


def test_create_widget_token_returns_token_and_expiry() -> None:
    channel = _channel()
    token, expires_at = create_widget_token(
        channel=channel, external_user_id="user_42"
    )
    assert isinstance(token, str)
    assert len(token) > 20
    # expiry should be ~30 min in the future
    delta = (expires_at - datetime.now(UTC)).total_seconds()
    assert WIDGET_TOKEN_TTL_SECONDS - 5 < delta <= WIDGET_TOKEN_TTL_SECONDS + 1


def test_widget_token_contains_required_claims() -> None:
    channel = _channel()
    token, _ = create_widget_token(channel=channel, external_user_id="u_42")
    payload = decode_token(token)
    assert payload["sub"] == "u_42"
    assert payload["channel_id"] == channel.id
    assert payload["tenant_id"] == channel.tenant_id
    assert payload["typ"] == "widget"


def test_decode_widget_token_rejects_non_widget_token() -> None:
    """A regular admin token (typ != 'widget') should not decode as a widget token."""
    admin_token = create_access_token(
        tenant_id="t1",
        user_id="admin",
        role="admin",
        extra={"typ": "access"},
    )
    # Sanity check: an admin token has typ=access, not widget
    decoded = decode_token(admin_token)
    assert decoded["typ"] == "access"
    with pytest.raises(TokenError, match="not a widget token"):
        decode_widget_token(admin_token)


def test_decode_widget_token_accepts_widget_token() -> None:
    channel = _channel()
    token, _ = create_widget_token(channel=channel, external_user_id="u_7")
    payload = decode_widget_token(token)
    assert payload["sub"] == "u_7"
    assert payload["channel_id"] == channel.id


def test_decode_widget_token_rejects_garbage() -> None:
    with pytest.raises(TokenError):
        decode_widget_token("not.a.real.jwt")
