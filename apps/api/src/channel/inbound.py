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

Streaming (``message.delta`` frames)
-------------------------------------
During an AI auto-response the pipeline relays each streamed text chunk as a
``message.delta`` frame ahead of the (unchanged) ``message.complete``. The
final ``message.complete`` still carries the persisted row id so the frontend
can finalise / dedupe the in-flight bubble against a later REST fetch. Deltas
carry only ``conversation_id`` + ``text``; the bubble is keyed by
``conversation_id`` because the row id does not exist yet while streaming.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from agent.simple_responder import SimpleResponder
from channel.messages import MessageEnvelope
from conversation.enums import ConversationStatus, MessageRole
from conversation.service import ConversationService
from core.logging import get_logger
from qa.worker import build_arq_redis
from ticket.repository import TicketRepository
from ticket.service import TicketService


def _build_ticket_service_factory() -> Callable[[], TicketService]:
    """Build a lazy ``TicketService`` factory for the customer-inbound hot path.

    The factory is invoked from inside
    :meth:`ConversationService.record_message` only when a customer
    message is being recorded. Each invocation opens its own
    short-lived DB session via :func:`core.database.get_session` so
    the ticket creation commits independently of the message insert
    — a ticket-creation failure must NOT fail the customer message
    ingest (the customer's turn is the product).

    Returns a callable rather than a pre-built ``TicketService``
    because ``TicketRepository`` requires a session at construction
    time; deferring construction until the first customer message
    means we don't open a DB session at module import / process
    start.
    """
    def _factory() -> TicketService:
        # ``get_session()`` opens its own short-lived session that
        # commits on exit. ``TicketService.create`` will call
        # ``repo.create`` (which flushes) — when the session exits
        # the commit fires and the ticket is durable. Subsequent
        # ``get_for_conversation`` queries on the same session are
        # fine because ``get_session()`` is called fresh each time.
        # We use the sessionmaker directly so the
        # ``TicketRepository`` can take an explicit session.
        from core.database import get_sessionmaker

        session = get_sessionmaker()()
        return TicketService(
            repo=TicketRepository(session),
            sla_policy_default_minutes=60,
        )

    return _factory


# The shared process-wide connection table. Imported by reference (not
# instantiated here) so that widget.ws.router — which owns the connection
# lifecycle — and this module broadcast into the *same* set of sockets.
from widget.ws.manager import manager as _wsm

logger = logging.getLogger(__name__)


async def _broadcast_ai_delta(
    *,
    channel_id: str,
    conversation_id: str,
    text: str,
) -> None:
    """Best-effort WS broadcast of a ``message.delta`` text chunk.

    Deliberately lightweight: the bubble is keyed by ``conversation_id`` (the
    AI row does not exist yet mid-stream), and the final
    ``message.complete`` — which *does* carry the real ``message_id`` — is
    what the frontend uses to finalise and dedupe. Wrapped in try/except so a
    dead socket never breaks the inbound path.
    """
    try:
        await _wsm.broadcast_to_channel(
            channel_id=channel_id,
            payload={
                "type": "message.delta",
                "conversation_id": conversation_id,
                "text": text,
            },
        )
    except Exception:
        logger.warning(
            "channel inbound: WS delta broadcast failed",
            extra={
                "channel_id": channel_id,
                "conversation_id": conversation_id,
            },
            exc_info=True,
        )


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


async def _enqueue_qa_judge(*, message_id: str, tenant_id: str) -> None:
    """Stage 14 / Task 8 — enqueue a QA judge task for one AI message.

    Best-effort: a Redis outage must NEVER fail the customer-facing
    webhook. The AI message is already durably persisted at this
    point; if Redis is down, the judge job is simply lost (the
    scoring is an observability signal, not a correctness
    requirement on the customer path). An operator can replay
    scoring by re-enqueueing past message ids via a Stage 15+
    backfill script.

    ``ArqRedis.from_url`` is invoked per-call so we don't carry a
    long-lived Redis pool across the inbound hot path (the same
    pattern as ``core.redis.get_redis`` — the connection itself is
    lazy and reuses one socket once ``enqueue_job`` is awaited).
    """
    try:
        arq_redis = build_arq_redis()
    except Exception as exc:
        logger.warning(
            "channel inbound: QA enqueue setup failed",
            extra={
                "message_id": message_id,
                "tenant_id": tenant_id,
                "error_type": type(exc).__name__,
            },
        )
        return
    try:
        await arq_redis.enqueue_job("qa_judge_task", message_id)
    except Exception as exc:
        logger.warning(
            "channel inbound: QA enqueue failed",
            extra={
                "message_id": message_id,
                "tenant_id": tenant_id,
                "error_type": type(exc).__name__,
            },
        )
        return


async def process_inbound_envelope(envelope: MessageEnvelope) -> None:
    """Find-or-create the conversation for the inbound envelope and persist the message.

    Best-effort: any error is logged and swallowed so the caller (webhook) can
    always return 200 to the channel provider. The channel provider's payload
    is the source of truth; retrying on our DB error would cause duplicate
    customer messages on eventual recovery.

    Returns nothing. A ``None`` from ``find_or_create_for_inbound`` indicates
    the cross-tenant guard tripped — we log and drop.
    """
    try:
        conv_service = ConversationService(
            # Task 6 (M2.A): inject a LAZY ticket-service factory so the
            # FIRST CUSTOMER message in each conversation auto-creates a
            # Ticket. The factory is invoked from inside
            # ``ConversationService.record_message`` only when a customer
            # message is being recorded — agent-reply and escalation paths
            # never trigger it.
            #
            # The factory opens its own short-lived DB session via
            # ``get_session()`` so the ticket creation commits
            # independently of the message insert (a ticket-creation
            # failure must NOT fail the customer message ingest).
            ticket_service_factory=_build_ticket_service_factory(),
        )
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
            channel_id = envelope.channel_id

            async def _stream_delta(text: str) -> None:
                # Relay each streamed chunk as a message.delta frame. The
                # callback signature matches the graph's ``on_delta`` contract.
                await _broadcast_ai_delta(
                    channel_id=channel_id,
                    conversation_id=conversation.id,
                    text=text,
                )

            ai_response = await responder.respond(
                tenant_id=envelope.tenant_id,
                conversation_id=conversation.id,
                on_delta=_stream_delta,
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
                # Stage 14 / Task 8 — non-blocking enqueue of the QA
                # judge task. Wrapped by ``_enqueue_qa_judge`` so a
                # Redis outage can't fail the customer webhook.
                await _enqueue_qa_judge(
                    message_id=ai_message.id,
                    tenant_id=envelope.tenant_id,
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
