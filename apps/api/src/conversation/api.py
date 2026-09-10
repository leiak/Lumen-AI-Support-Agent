"""Conversation management API.

All routes under /api/v1/conversations, all behind JWT auth:
- admin (or owner) role required for state-changing and most read endpoints
- agent role can hit /inbox to see their assigned conversations
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from auth.jwt import TokenError, decode_token
from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation, Message
from conversation.service import ConversationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


# ---- Auth dependencies --------------------------------------------------

async def _decode_jwt(authorization: str | None) -> dict[str, Any]:
    """Decode Bearer JWT or raise 401."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[len("Bearer "):]
    try:
        return decode_token(token)
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


async def require_admin(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """JWT auth — admin or owner role required."""
    claims = await _decode_jwt(authorization)
    role = claims.get("role")
    if role not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="admin role required")
    if "tenant_id" not in claims:
        raise HTTPException(status_code=401, detail="token missing tenant_id")
    return claims


async def require_agent_or_admin(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """JWT auth — agent, admin, or owner allowed."""
    claims = await _decode_jwt(authorization)
    role = claims.get("role")
    if role not in ("agent", "admin", "owner"):
        raise HTTPException(status_code=403, detail="agent or admin role required")
    if "tenant_id" not in claims:
        raise HTTPException(status_code=401, detail="token missing tenant_id")
    if not claims.get("sub"):
        raise HTTPException(status_code=401, detail="token missing sub claim")
    return claims


# ---- Schemas ------------------------------------------------------------

class AssignRequest(BaseModel):
    """POST /conversations/{id}/assign body."""

    agent_id: str = Field(..., min_length=1, max_length=26)


class ConversationOut(BaseModel):
    """Public conversation representation."""

    id: str
    tenant_id: str
    channel_id: str
    customer_external_id: str
    status: ConversationStatus
    assigned_agent_id: str | None
    ai_handling: bool
    opened_at: datetime
    last_activity_at: datetime


class MessageOut(BaseModel):
    """Public message representation."""

    id: str
    conversation_id: str
    role: MessageRole
    content_text: str
    sender_id: str | None
    created_at: datetime


class ConversationListOut(BaseModel):
    """List response. No total — list endpoints don't run a separate count query."""

    items: list[ConversationOut]


class MessageListOut(BaseModel):
    """Paginated messages response."""

    items: list[MessageOut]


# ---- Mappers ------------------------------------------------------------

def _conv_out(c: Conversation) -> ConversationOut:
    return ConversationOut(
        id=c.id,
        tenant_id=c.tenant_id,
        channel_id=c.channel_id,
        customer_external_id=c.customer_external_id,
        status=c.status,
        assigned_agent_id=c.assigned_agent_id,
        ai_handling=c.ai_handling,
        opened_at=c.opened_at,
        last_activity_at=c.last_activity_at,
    )


def _msg_out(m: Message) -> MessageOut:
    return MessageOut(
        id=m.id,
        conversation_id=m.conversation_id,
        role=m.role,
        content_text=m.content_text,
        sender_id=m.sender_id,
        created_at=m.created_at,
    )


def _service() -> ConversationService:
    return ConversationService()


# ---- Routes -------------------------------------------------------------

@router.get("", response_model=ConversationListOut)
async def list_conversations(
    claims: Annotated[dict[str, Any], Depends(require_admin)],
    status_filter: Annotated[ConversationStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConversationListOut:
    """List conversations for the caller's tenant (admin only)."""
    items = await _service().list_for_tenant(
        tenant_id=claims["tenant_id"],
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    # NOTE: For M1 we don't paginate by total count. The service returns at
    # most `limit` rows. Frontend can paginate by adjusting offset. If a
    # `total` is required, add a separate count query.
    return ConversationListOut(items=[_conv_out(c) for c in items])


@router.get("/inbox", response_model=ConversationListOut)
async def list_inbox(
    claims: Annotated[dict[str, Any], Depends(require_agent_or_admin)],
    status_filter: Annotated[ConversationStatus | None, Query(alias="status")] = None,
) -> ConversationListOut:
    """List conversations assigned to the calling agent.

    Agent id is taken from the JWT `sub` claim. Admins can hit this endpoint
    too — they see conversations assigned to *their own* user id.
    """
    items = await _service().list_for_agent(
        tenant_id=claims["tenant_id"],
        agent_id=claims["sub"],
        status=status_filter,
    )
    return ConversationListOut(items=[_conv_out(c) for c in items])


@router.get("/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    conversation_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> ConversationOut:
    """Fetch a single conversation. 404 if not visible to this tenant."""
    conv = await _service().get(
        tenant_id=claims["tenant_id"], conversation_id=conversation_id
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return _conv_out(conv)


@router.get("/{conversation_id}/messages", response_model=MessageListOut)
async def list_messages(
    conversation_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
    before: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> MessageListOut:
    """List messages for a conversation in chronological order.

    Pagination: if `before` is supplied, returns messages strictly older
    than that timestamp. Otherwise returns the latest `limit` messages.
    """
    msgs = await _service().list_messages(
        tenant_id=claims["tenant_id"],
        conversation_id=conversation_id,
        before=before,
        limit=limit,
    )
    if msgs is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return MessageListOut(items=[_msg_out(m) for m in msgs])


@router.post("/{conversation_id}/assign", response_model=ConversationOut)
async def assign_conversation(
    conversation_id: str,
    body: AssignRequest,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> ConversationOut:
    """Transfer a conversation to a human agent. Status -> PENDING."""
    conv = await _service().assign_to_agent(
        tenant_id=claims["tenant_id"],
        conversation_id=conversation_id,
        agent_id=body.agent_id,
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return _conv_out(conv)


@router.post("/{conversation_id}/return-to-ai", response_model=ConversationOut)
async def return_to_ai(
    conversation_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> ConversationOut:
    """Return control of a conversation to the AI agent."""
    conv = await _service().return_to_ai(
        tenant_id=claims["tenant_id"],
        conversation_id=conversation_id,
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return _conv_out(conv)


@router.post("/{conversation_id}/close", response_model=ConversationOut)
async def close_conversation(
    conversation_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> ConversationOut:
    """Close a conversation. Status -> CLOSED."""
    conv = await _service().close(
        tenant_id=claims["tenant_id"],
        conversation_id=conversation_id,
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return _conv_out(conv)