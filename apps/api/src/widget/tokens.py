"""Widget JWT issuance for the embedded web chat widget.

Widget tokens authenticate end-users (anonymous visitors on a customer's
website) so they can open a WebSocket session to the AI gateway. They are
short-lived (30 min by default), bound to a specific channel and an opaque
external_user_id supplied by the embedding site, and carry `typ="widget"`
to distinguish them from admin login tokens.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import jwt

from auth.jwt import TokenError, decode_token
from channel.models import Channel
from core.config import get_settings

logger = logging.getLogger(__name__)

WIDGET_TOKEN_TTL_SECONDS = 30 * 60  # 30 minutes
WIDGET_TOKEN_REFRESH_GRACE_SECONDS = 5 * 60  # 5 minutes past expiry for refresh


def create_widget_token(
    *,
    channel: Channel,
    external_user_id: str,
    ttl_seconds: int = WIDGET_TOKEN_TTL_SECONDS,
) -> tuple[str, datetime]:
    """Issue a widget session JWT bound to a channel + external_user_id.

    Returns (token, expires_at).
    """
    settings = get_settings()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_seconds)
    payload: dict[str, Any] = {
        "sub": external_user_id,
        "channel_id": channel.id,
        "tenant_id": channel.tenant_id,
        "typ": "widget",
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token: str = jwt.encode(
        payload, settings.jwt_secret, algorithm=settings.jwt_algorithm
    )
    return token, expires_at


def decode_widget_token(token: str) -> dict[str, Any]:
    """Decode a widget token, enforcing `typ == "widget"`.

    Raises TokenError on bad signature, expiry, malformed payload, or wrong type.
    """
    payload = decode_token(token)
    if payload.get("typ") != "widget":
        raise TokenError("token is not a widget token")
    return payload
