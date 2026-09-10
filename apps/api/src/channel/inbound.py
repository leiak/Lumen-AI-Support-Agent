"""Channel-agnostic inbound processor.

Bridges the MessageEnvelope emitted by channel adapters with the
ConversationService: ensures a conversation exists for the (channel, customer)
pair and persists the inbound message.

The processor is best-effort: any error is logged and swallowed so the
caller (webhook or WS handler) can always return 200 to the channel
provider. The channel provider's payload is the source of truth; retrying
on our DB error would cause duplicate customer messages on eventual
recovery.
"""
from __future__ import annotations

import logging

from channel.messages import MessageEnvelope
from conversation.enums import MessageRole
from conversation.service import ConversationService

logger = logging.getLogger(__name__)


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
            # Cross-tenant guard tripped — log and drop.
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
        logger.info(
            "channel inbound: message recorded",
            extra={
                "conversation_id": conversation.id,
                "envelope_id": envelope.envelope_id,
                "channel_id": envelope.channel_id,
            },
        )
    except Exception:
        logger.exception(
            "channel inbound: persistence failed",
            extra={
                "envelope_id": envelope.envelope_id,
                "channel_id": envelope.channel_id,
            },
        )
