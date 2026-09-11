"""Conversation management API.

All routes under /api/v1/conversations, all behind JWT auth:
- admin (or owner) role required for state-changing and most read endpoints
- agent role can hit /inbox to see their assigned conversations
- agent role can POST /conversations/{id}/messages (Stage 8.1)

Role-gating dependencies are imported from ``auth.dependencies`` so the
JWT decode + role enforcement logic lives in one place.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from auth.dependencies import require_admin, require_agent_or_admin
from agent.schemas import AgentMessageCreate
from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation, Message
from conversation.service import ConversationService
from core.logging import get_logger
from widget.ws.manager import manager as _ws_manager

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])

log = get_logger(__name__)


async def _broadcast_agent_message(
    *,
    channel_id: str,
    conversation_id: str,
    message_id: str,
    sender_id: str | None,
) -> None:
    """Best-effort WS broadcast of an agent ``message.created`` event.

    Mirrors the Stage 5.3 ``message.complete`` fan-out used by the
    customer-inbound path. Wrapped in try/except so a dead socket
    never aborts the agent-reply hot path — the message is already
    durably persisted and the client can refetch via REST.
    """
    try:
        await _ws_manager.broadcast_to_channel(
            channel_id=channel_id,
            payload={
                "type": "message.created",
                "conversation_id": conversation_id,
                "message_id": message_id,
                "role": MessageRole.AGENT.value,
                "sender_id": sender_id,
            },
        )
    except Exception as exc:
        log.warning(
            "agent message broadcast failed",
            conversation_id=conversation_id,
            channel_id=channel_id,
            message_id=message_id,
            error_type=type(exc).__name__,
        )


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


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_agent_message(
    conversation_id: str,
    body: AgentMessageCreate,
    claims: Annotated[dict[str, Any], Depends(require_agent_or_admin)],
) -> MessageOut:
    """Post a message AS the calling agent (Stage 8.1 reply box).

    Accepts the ``agent`` role in addition to admin/owner so assigned
    agents can reply from the workspace. Tenant isolation is enforced
    inside ``ConversationService.record_message`` — cross-tenant or
    unknown conversation ids raise ``ValueError`` which we map to 404
    to keep the anti-enumeration contract.

    Persisting the message also advances ``last_activity_at`` via the
    service. Broadcasting a ``message.created`` WS event happens AFTER
    persist so subscribers never see an event for a non-durable row.
    """
    stripped = body.stripped_text
    if not stripped:
        # Pydantic ``min_length`` would also catch this but only for the
        # raw string; an all-whitespace payload passes schema validation.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="content_text must not be empty",
        )
    tenant_id = claims["tenant_id"]
    sender_id = claims["sub"]
    try:
        msg = await _service().record_message(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            role=MessageRole.AGENT,
            content_text=stripped,
            sender_id=sender_id,
        )
    except ValueError:
        # Cross-tenant or unknown — same 404 the rest of the API uses
        # to prevent enumeration via response differentiation.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found"
        ) from None
    # Look up the channel so we know where to broadcast. Done after
    # persist so a broadcast failure cannot turn into a missing message.
    conv = await _service().get(tenant_id=tenant_id, conversation_id=conversation_id)
    if conv is not None:
        await _broadcast_agent_message(
            channel_id=conv.channel_id,
            conversation_id=conversation_id,
            message_id=msg.id,
            sender_id=sender_id,
        )
    log.info(
        "agent message recorded",
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        sender_id=sender_id,
        message_id=msg.id,
    )
    return _msg_out(msg)


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