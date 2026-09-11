"""Unified tenant channel management API.

Five endpoints under `/api/v1/channels`, all behind admin JWT auth:

- POST   /api/v1/channels               — create a new channel for the tenant
- GET    /api/v1/channels               — list all channels for the tenant
- GET    /api/v1/channels/{channel_id}  — get one channel
- PATCH  /api/v1/channels/{channel_id}  — update name/status/credentials
- DELETE /api/v1/channels/{channel_id}  — soft-disable (status=DISABLED)

The ``require_admin`` dependency is imported from ``auth.dependencies`` so
the JWT decode + role enforcement logic lives in one place. Tenant
context is taken from the JWT's ``tenant_id`` claim; there is no way
for a caller to act on a different tenant's channels.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from auth.dependencies import require_admin
from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from channel.service import ChannelService

router = APIRouter(prefix="/api/v1/channels", tags=["channels"])


# ---- Schemas ------------------------------------------------------------
class ChannelCreate(BaseModel):
    """POST /api/v1/channels body."""

    type: ChannelType
    name: str = Field(..., min_length=1, max_length=200)
    credentials: dict[str, Any] = Field(
        ...,
        description=(
            "Arbitrary JSON credentials for the channel type "
            "(e.g. Feishu app_id/app_secret). Never echoed back in responses."
        ),
    )


class ChannelUpdate(BaseModel):
    """PATCH /api/v1/channels/{id} body. All fields optional."""

    name: str | None = Field(None, min_length=1, max_length=200)
    status: ChannelStatus | None = None
    credentials: dict[str, Any] | None = None


class ChannelOut(BaseModel):
    """Response model — metadata only, NEVER includes credentials."""

    id: str
    type: ChannelType
    name: str
    status: ChannelStatus
    tenant_id: str
    created_at: datetime


def _channel_out(ch: Channel) -> ChannelOut:
    """Map a Channel ORM row to its public response (drops credentials)."""
    return ChannelOut(
        id=ch.id,
        type=ch.type,
        name=ch.name,
        status=ch.status,
        tenant_id=ch.tenant_id,
        created_at=ch.created_at,
    )


def _service() -> ChannelService:
    """Build a fresh service. Cheap to construct (one repo, no I/O)."""
    return ChannelService()


# ---- Routes -------------------------------------------------------------
@router.post("", response_model=ChannelOut, status_code=status.HTTP_201_CREATED)
async def create_channel(
    body: ChannelCreate,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> ChannelOut:
    """Create a new ACTIVE channel for the caller's tenant."""
    ch = await _service().create(
        tenant_id=claims["tenant_id"],
        type=body.type,
        name=body.name,
        credentials=body.credentials,
    )
    return _channel_out(ch)


@router.get("", response_model=list[ChannelOut])
async def list_channels(
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> list[ChannelOut]:
    """List all channels for the caller's tenant."""
    channels = await _service().list(tenant_id=claims["tenant_id"])
    return [_channel_out(c) for c in channels]


@router.get("/{channel_id}", response_model=ChannelOut)
async def get_channel(
    channel_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> ChannelOut:
    """Fetch a single channel by id (tenant-scoped). 404 if not visible."""
    ch = await _service().get(tenant_id=claims["tenant_id"], channel_id=channel_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="channel not found")
    return _channel_out(ch)


@router.patch("/{channel_id}", response_model=ChannelOut)
async def update_channel(
    channel_id: str,
    body: ChannelUpdate,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> ChannelOut:
    """Patch mutable fields. Any omitted field is left unchanged."""
    ch = await _service().update(
        tenant_id=claims["tenant_id"],
        channel_id=channel_id,
        name=body.name,
        status=body.status,
        credentials=body.credentials,
    )
    if ch is None:
        raise HTTPException(status_code=404, detail="channel not found")
    return _channel_out(ch)


@router.delete("/{channel_id}", response_model=ChannelOut)
async def delete_channel(
    channel_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> ChannelOut:
    """Soft-disable a channel (status -> DISABLED). 404 if not visible."""
    ch = await _service().soft_delete(
        tenant_id=claims["tenant_id"], channel_id=channel_id
    )
    if ch is None:
        raise HTTPException(status_code=404, detail="channel not found")
    return _channel_out(ch)