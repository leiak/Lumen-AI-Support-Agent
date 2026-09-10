"""Channel-agnostic inbound processor.

Bridges the MessageEnvelope emitted by channel adapters with the
ConversationService: ensures a conversation exists for the (channel, customer)
pair, persists the inbound message, triggers an AI auto-response if applicable,
and broadcasts a ``message.complete`` event to any connected widget clients.

The processor is best-effort: any error is logged and swallowed so the
caller (webhook or WS handler) can always return 200 to the channel
provider. The channel provider's payload is the source of truth; retrying
on our DB error would cause duplicate customer messages on eventual
recovery.

M1 limitation — no token streaming
----------------------------------
We emit a single ``message.complete`` frame *after* the AI message is
persisted; there are no ``message.delta`` frames. The M1 LLM client does not
implement streaming (``AnthropicProvider.stream`` raises NotImplementedError),
so there is nothing incremental to forward. The event envelope is shaped so
Stage 7 can add ``message.delta`` frames ahead of the existing
``message.complete`` without changing the completion contract.
"""
from __future__ import annotations

import logging

from agent.simple_responder import SimpleResponder
from channel.messages import MessageEnvelope
from conversation.enums import ConversationStatus, MessageRole
from conversation.service import ConversationService

# The shared process-wide connection table. Imported by reference (not
# instantiated here) so that widget.ws.router — which owns the connection
# lifecycle — and this module broadcast into the *same* set of sockets.
from widget.ws.manager import manager as _wsm

logger = logging.getLogger(__name__)


async def _broadcast_ai_complete(
    *,
    channel_id: str,
    conversation_id: str,
    message_id: str,
    role: MessageRole,
    content: str,
) -> None:
    """Best-effort WS broadcast of an AI ``message.complete`` event.

    Wrapped in try/except so a WS failure (dead socket, serialisation error)
    never breaks the inbound path — the message is already durably persisted,
    and the client can recover the missed event via the REST message list.

    ``message_id`` is the *persisted* row id, which lets the frontend dedupe
    this push against a subsequent REST fetch.
    """
    try:
        await _wsm.broadcast_to_channel(
            channel_id=channel_id,
            payload={
                "type": "message.complete",
                "conversation_id": conversation_id,
                "message_id": message_id,
                "role": role.value,
                "content": content,
            },
        )
    except Exception:
        logger.warning(
            "channel inbound: WS broadcast failed",
            extra={
                "channel_id": channel_id,
                "conversation_id": conversation_id,
                "message_id": message_id,
            },
            exc_info=True,
        )


async def process_inbound_envelope(envelope: MessageEnvelope) -> None:
    """Find-or-create the conversation for the inbound envelope and persist the message.

    Best-effort: any error is logged and swallowed so the caller (webhook) can
    always return 200 to the channel provider. The channel provider's payload
    is the source of truth; retrying on our DB error would cause duplicate
    customer messages on eventual recovery.

    Returns nothing. A ``None`` from ``find_or_create_for_inbound`` indicates
    the cross-tenant guard tripped — we log and drop.
    """
    conv_service = ConversationService()
    try:
        conversation = await conv_service.find_or_create_for_inbound(
            tenant_id=envelope.tenant_id,
            channel_id=envelope.channel_id,
            customer_external_id=envelope.external_user_id,
        )
        if conversation is None:
            # Cross-tenant guard tripped — log and drop. No broadcast: we
            # must not leak the existence of another tenant's conversation.
            logger.warning(
                "channel inbound: cross-tenant probe blocked",
                extra={
                    "channel_id": envelope.channel_id,
                    "envelope_id": envelope.envelope_id,
                },
            )
            return
        await conv_service.record_message(
            tenant_id=envelope.tenant_id,
            conversation_id=conversation.id,
            role=MessageRole.CUSTOMER,
            content_text=envelope.text,
        )
        # Trigger AI auto-response when the conversation is still in
        # AI-handling state. Transferred / closed conversations fall
        # through silently. Inline call is intentional for M1 demo
        # volume; Stage 7+ should move this to a background worker.
        if conversation.ai_handling and conversation.status == ConversationStatus.OPEN:
            responder = SimpleResponder()
            ai_response = await responder.respond(
                tenant_id=envelope.tenant_id,
                conversation_id=conversation.id,
            )
            if ai_response is not None:
                ai_message = await conv_service.record_message(
                    tenant_id=envelope.tenant_id,
                    conversation_id=conversation.id,
                    role=ai_response.role,
                    content_text=ai_response.content_text,
                )
                # Push to any widget clients subscribed to this channel.
                # Deliberately after the persist so the event carries a real
                # row id and can never describe a message that isn't durable.
                await _broadcast_ai_complete(
                    channel_id=envelope.channel_id,
                    conversation_id=conversation.id,
                    message_id=ai_message.id,
                    role=ai_response.role,
                    content=ai_response.content_text,
                )
                logger.info(
                    "channel inbound: AI auto-response recorded",
                    extra={
                        "conversation_id": conversation.id,
                        "channel_id": envelope.channel_id,
                    },
                )
        logger.info(
            "channel inbound: message recorded",
            extra={
                "conversation_id": conversation.id,
                "envelope_id": envelope.envelope_id,
                "channel_id": envelope.channel_id,
            },
        )
    except Exception:
        logger.warning(
            "channel inbound: persistence failed",
            extra={
                "envelope_id": envelope.envelope_id,
                "channel_id": envelope.channel_id,
            },
            exc_info=True,
        )
