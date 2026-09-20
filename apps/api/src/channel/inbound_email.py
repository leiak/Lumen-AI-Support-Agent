"""Email inbound webhook handler.

Mirrors channel.inbound.process_inbound_envelope but takes a parsed
SES payload + tenant context. Thread-routing via email_thread_id on
Conversation. Idempotent on email_message_id_header (SES retries).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from channel.outbound_email import EmailOutbound, EmailSendError
from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation
from conversation.service import ConversationService
from core.database import get_sessionmaker
from core.email_parser import ParsedEmail
from core.id_gen import new_id

logger = logging.getLogger(__name__)


@dataclass
class EmailInboundResult:
    conversation_id: str
    message_id: str  # customer message id (empty string on duplicate)
    ai_message_id: str | None
    ai_content: str | None  # returned for the caller to send via outbound
    is_duplicate: bool = False  # True when the message_id was already seen


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
) -> Conversation | None:
    """Return the existing Conversation for this message_id_header, or None.

    The dedup check returns the row (not just a bool) so the caller can
    include ``conversation_id`` in the duplicate response — useful for
    the SES retry caller to correlate the duplicate with the original
    conversation.
    """
    from sqlalchemy import select

    result = await session.execute(
        select(Conversation)
        .where(Conversation.email_message_id_header == message_id_header)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def handle_email_inbound(
    *,
    tenant_id: str,
    parsed: ParsedEmail,
    channel_id: str,  # the EmailChannel row id
    responder_factory,  # callable: () -> SimpleResponder
    outbound: EmailOutbound | None = None,  # for sending AI auto-reply
) -> EmailInboundResult | None:
    """Process an inbound email end-to-end.

    Returns ``EmailInboundResult`` with ``is_duplicate=True`` when the
    ``email_message_id_header`` was already seen (idempotent dedup) or
    when a concurrent webhook beat us to INSERT (UNIQUE race). The
    caller maps that to ``status="duplicate"`` while keeping the
    correlation ``conversation_id`` for SES-retry observability.

    Returns ``None`` only when an unhandled DB step raises (logged at
    ERROR) — the webhook must always return 200 to SES, so the API
    layer surfaces ``None`` as ``{"status": "duplicate"}`` to keep
    SES from retry-storming.

    NOTEs on tenant auto-create divergence vs widget path:
    The widget path auto-creates a Ticket via ticket_service_factory on
    the first customer message (M2.A / Stage 13). The email path
    intentionally does NOT — emails are typically operational in nature
    and skip the Ticket workflow. Add ticket_service_factory wiring
    here if/when that policy changes.
    """
    sm = get_sessionmaker()
    try:
        async with sm() as session:
            # 1. Idempotency check — SES retries on 5xx
            existing = await _check_existing_message(
                session, message_id_header=parsed.message_id
            )
            if existing is not None:
                logger.info(
                    "email.inbound.duplicate",
                    extra={
                        "tenant_id": tenant_id,
                        "message_id": parsed.message_id,
                    },
                )
                # Return the conversation_id so the SES retry caller can
                # correlate the duplicate with the original conversation.
                # Empty message_id distinguishes "duplicate, nothing new
                # persisted" from "ok, here's the new message id".
                return EmailInboundResult(
                    conversation_id=existing.id,
                    message_id="",
                    ai_message_id=None,
                    ai_content=None,
                    is_duplicate=True,
                )

            # 2. Find or create conversation by email thread
            conversation = await _find_conversation_by_thread(
                session,
                tenant_id=tenant_id,
                thread_id=parsed.thread_id,
            )
            if conversation is None:
                # First email in thread — create conversation. opened_at
                # and last_activity_at are NOT NULL on the model (mirror
                # service.py:179-190 pattern).
                now = datetime.now(timezone.utc)
                conversation = Conversation(
                    id=new_id(),
                    tenant_id=tenant_id,
                    channel_id=channel_id,
                    customer_external_id=parsed.from_address,
                    status=ConversationStatus.OPEN,
                    ai_handling=True,
                    email_thread_id=parsed.thread_id,
                    email_message_id_header=parsed.message_id,
                    opened_at=now,
                    last_activity_at=now,
                )
                session.add(conversation)
                try:
                    await session.flush()
                except IntegrityError:
                    # Race: a concurrent webhook beat us to the INSERT
                    # (UNIQUE on email_message_id_header). Treat as duplicate.
                    logger.info(
                        "email.inbound.duplicate_race",
                        extra={
                            "tenant_id": tenant_id,
                            "message_id": parsed.message_id,
                        },
                    )
                    await session.rollback()
                    # Look up the winning conversation so the duplicate
                    # response can include its conversation_id.
                    winner = await _check_existing_message(
                        session, message_id_header=parsed.message_id
                    )
                    return EmailInboundResult(
                        conversation_id=winner.id if winner else "",
                        message_id="",
                        ai_message_id=None,
                        ai_content=None,
                        is_duplicate=True,
                    )
                # Commit the new conversation in its own transaction so the
                # subsequent ``conv_service.record_message(...)`` call —
                # which opens its OWN session via ``ConversationRepository``
                # — can see the row. Without this commit, the sub-session
                # used by ``record_message`` would observe the conversation
                # as not-yet-committed and raise ValueError, masking the
                # webhook handling as a 'duplicate' outcome.
                await session.commit()
            else:
                # Bump dedup key so a future retry with this same
                # message_id is caught by _check_existing_message above.
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
    except Exception as e:
        # Defense in depth: any uncaught error from the DB block must
        # not propagate as a 5xx (SES would retry-storm). Mirror the
        # widget path's outer try/except in channel/inbound.py:308-316.
        logger.error(
            "email.inbound.unhandled_error",
            extra={
                "tenant_id": tenant_id,
                "message_id": parsed.message_id,
                "error_type": type(e).__name__,
            },
        )
        return None

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
        except EmailSendError as send_err:
            # SES down — message is durably persisted; outbound retry
            # is a future task (M3). Log and return success.
            logger.warning(
                "email.inbound.outbound_send_failed",
                extra={
                    "tenant_id": tenant_id,
                    "message_id": parsed.message_id,
                    "error_type": send_err.error_type or "EmailSendError",
                },
            )

    return EmailInboundResult(
        conversation_id=conversation.id,
        message_id=customer_message.id,
        ai_message_id=ai_message_id,
        ai_content=ai_content,
    )
