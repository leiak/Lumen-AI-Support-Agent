"""Web Widget token issuance HTTP handlers.

Two public endpoints (no admin auth required) issue short-lived JWTs to
the embedded JavaScript widget:

- POST /api/v1/widget/token        — first-time issue for a channel + user
- POST /api/v1/widget/token/refresh — re-issue before the existing token
  expires, without dropping the WebSocket connection

Both endpoints enforce that the channel exists and is ACTIVE. The refresh
endpoint additionally verifies the previous token's signature and that
its `channel_id` / `sub` match the request body (prevents token theft
across channels or users).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from auth.jwt import TokenError
from channel.enums import ChannelStatus
from channel.repository import ChannelRepository
from widget.tokens import create_widget_token, decode_widget_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/widget", tags=["widget"])


# TODO(Stage 5+): widget endpoints are public and need per-IP rate limiting
# at the edge (NGINX/Cloudflare) or via a Redis-backed FastAPI middleware.
# An attacker can otherwise flood /token with random channel_ids and force-sign tokens.


class TokenRequest(BaseModel):
    channel_id: str = Field(..., min_length=1, max_length=64)
    external_user_id: str = Field(..., min_length=1, max_length=200)


class TokenResponse(BaseModel):
    token: str
    expires_at: datetime
    expires_in: int  # seconds until expiry


class RefreshRequest(BaseModel):
    channel_id: str = Field(..., min_length=1, max_length=64)
    external_user_id: str = Field(..., min_length=1, max_length=200)
    previous_token: str = Field(..., min_length=1)


@router.post("/token", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def issue_widget_token(body: TokenRequest) -> TokenResponse:
    """Issue a fresh widget session JWT.

    Returns 404 if the channel is unknown, 403 if the channel is not ACTIVE.
    """
    repo = ChannelRepository()
    channel = await repo.get_by_id(body.channel_id)
    if channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="channel not found"
        )
    if channel.status != ChannelStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="channel is not active"
        )
    token, expires_at = create_widget_token(
        channel=channel, external_user_id=body.external_user_id
    )
    expires_in = int((expires_at - datetime.now(UTC)).total_seconds())
    return TokenResponse(token=token, expires_at=expires_at, expires_in=expires_in)


@router.post("/token/refresh", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def refresh_widget_token(body: RefreshRequest) -> TokenResponse:
    """Re-issue a widget session JWT, validating the previous token.

    M1 best-effort: validates the existing token via `decode_widget_token`
    (which enforces signature + expiry + `typ == "widget"`). Full expiry
    grace (up to WIDGET_TOKEN_REFRESH_GRACE_SECONDS past `exp`) is a
    Stage 5+ concern.
    """
    try:
        payload = decode_widget_token(body.previous_token)
    except TokenError as exc:
        logger.warning("widget refresh rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid previous token"
        ) from exc

    if payload.get("channel_id") != body.channel_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="channel mismatch"
        )
    if payload.get("sub") != body.external_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="user mismatch"
        )

    repo = ChannelRepository()
    channel = await repo.get_by_id(body.channel_id)
    if channel is None or channel.status != ChannelStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="channel is not active"
        )
    token, expires_at = create_widget_token(
        channel=channel, external_user_id=body.external_user_id
    )
    expires_in = int((expires_at - datetime.now(UTC)).total_seconds())
    return TokenResponse(token=token, expires_at=expires_at, expires_in=expires_in)
