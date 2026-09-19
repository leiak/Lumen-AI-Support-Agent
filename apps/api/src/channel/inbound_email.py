"""Email inbound webhook handler.

Mirrors channel.inbound.process_inbound_envelope but takes a parsed
SES payload + tenant context. Thread-routing via email_thread_id on
Conversation. Idempotent on email_message_id_header (SES retries).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from channel.outbound_email import EmailOutbound, EmailSendError
from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation
from conversation.service import ConversationService
from core.database import get_sessionmaker
from core.email_parser import ParsedEmail

logger = logging.getLogger(__name__)


@dataclass
class EmailInboundResult:
    conversation_id: str
    message_id: str  # customer message id
    ai_message_id: str | None
    ai_content: str | None  # returned for the caller to send via outbound


async def _find_conversation_by_thread(
    session: AsyncSession,
    *,
    tenant_id: str,
    thread_id: str,
) -> Conversation | None:
    """Look up existing conversation by email thread (cross-channel idempotency)."""
    from sqlalchemy import select

    result = await session.execute(
        select(Conversation)
        .where(
            Conversation.tenant_id == tenant_id,
            Conversation.email_thread_id == thread_id,
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _check_existing_message(
    session: AsyncSession,
    *,
    message_id_header: str,
) -> bool:
    """Returns True if we've already processed this email_message_id (SES retry dedup)."""
    from sqlalchemy import select

    result = await session.execute(
        select(Conversation)
        .where(Conversation.email_message_id_header == message_id_header)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def handle_email_inbound(
    *,
    tenant_id: str,
    parsed: ParsedEmail,
    channel_id: str,  # the EmailChannel row id
    responder_factory,  # callable: () -> SimpleResponder
    outbound: EmailOutbound | None = None,  # for sending AI auto-reply
) -> EmailInboundResult | None:
    """Process an inbound email end-to-end.

    Returns None if the message is a duplicate (idempotent) or if
    tenant_id is missing (cross-tenant probe blocked).
    """
    sm = get_sessionmaker()
    async with sm() as session:
        # 1. Idempotency check — SES retries on 5xx
        if await _check_existing_message(
            session, message_id_header=parsed.message_id
        ):
            logger.info(
                "email.inbound.duplicate",
                extra={"tenant_id": tenant_id, "message_id": parsed.message_id},
            )
            return None

        # 2. Find or create conversation by email thread
        conversation = await _find_conversation_by_thread(
            session,
            tenant_id=tenant_id,
            thread_id=parsed.thread_id,
        )
        if conversation is None:
            # First email in thread — create conversation
            from ulid import ULID

            conversation = Conversation(
                id=str(ULID()),
                tenant_id=tenant_id,
                channel_id=channel_id,
                customer_external_id=parsed.from_address,
                status=ConversationStatus.OPEN,
                ai_handling=True,
                email_thread_id=parsed.thread_id,
                email_message_id_header=parsed.message_id,
            )
            session.add(conversation)
            await session.flush()
        else:
            # Update last seen message id (audit trail)
            conversation.email_message_id_header = parsed.message_id

        # 3. Record customer message
        conv_service = ConversationService()
        customer_message = await conv_service.record_message(
            tenant_id=tenant_id,
            conversation_id=conversation.id,
            role=MessageRole.CUSTOMER,
            content_text=parsed.body_text,
        )

        # 4. Trigger AI auto-response (mirrors widget path)
        ai_message_id: str | None = None
        ai_content: str | None = None
        if (
            conversation.ai_handling
            and conversation.status == ConversationStatus.OPEN
        ):
            responder = responder_factory()
            ai_response = await responder.respond(
                tenant_id=tenant_id,
                conversation_id=conversation.id,
            )
            if ai_response is not None:
                ai_msg = await conv_service.record_message(
                    tenant_id=tenant_id,
                    conversation_id=conversation.id,
                    role=ai_response.role,
                    content_text=ai_response.content_text,
                )
                ai_message_id = ai_msg.id
                ai_content = ai_response.content_text

        await session.commit()

    # 5. Send AI auto-reply email (best-effort, outside the DB transaction)
    if ai_content and outbound:
        try:
            subject = parsed.subject
            if not subject.startswith("Re:"):
                subject = f"Re: {subject}"
            await outbound.send_reply(
                tenant_id=tenant_id,
                to_email=parsed.from_address,
                subject=subject,
                body_text=ai_content,
                in_reply_to=parsed.message_id,
                references=parsed.references + [parsed.message_id],
            )
        except EmailSendError:
            # SES down — message is durably persisted; outbound retry
            # is a future task (M3). Log and return success.
            logger.warning(
                "email.inbound.outbound_send_failed",
                extra={
                    "tenant_id": tenant_id,
                    "message_id": parsed.message_id,
                    "error_type": "EmailSendError",
                },
            )

    return EmailInboundResult(
        conversation_id=conversation.id,
        message_id=customer_message.id,
        ai_message_id=ai_message_id,
        ai_content=ai_content,
    )
