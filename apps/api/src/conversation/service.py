"""Tenant-scoped conversation + message service.

This is the business-logic layer between the API layer (Task 5.5), the
channel adapters (Task 5.2), and the persistence layer
(`conversation/repository.py`). The service is responsible for:

- enforcing tenant scoping on every operation (a conversation from another
  tenant must never leak through `get`/`assign_to_agent`/etc.);
- driving the OPEN / PENDING / CLOSED state machine and the
  `ai_handling` / `assigned_agent_id` flags in lock-step;
- advancing `last_activity_at` whenever a message is recorded so the
  hot-path queries stay correct;
- returning `None` for not-found / cross-tenant access rather than
  raising, so the API layer can map to 404 uniformly (with the
  exception of `record_message`, which raises `ValueError` because the
  caller is the API or channel-adapter boundary and should fail loud
  on tenant mismatch).
- auto-creating a Ticket on the first CUSTOMER message in a
  conversation (Task 6, M2.A). The ``ticket_service`` is OPTIONAL —
  callers that don't pass one (e.g. agent-reply and escalation paths)
  simply skip the auto-create. Only ``channel.inbound`` injects a
  TicketService so the customer-inbound hot path generates tickets.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from conversation.enums import ConversationStatus, MessageRole
from conversation.exceptions import ConversationNotClaimableError
from conversation.models import Conversation, Message
from conversation.repository import ConversationRepository, MessageRepository
from core.business_metrics import MESSAGES_TOTAL
from core.database import get_session, get_sessionmaker
from core.id_gen import new_id

if TYPE_CHECKING:
    from ticket.service import TicketService

logger = logging.getLogger(__name__)


async def _try_auto_create_ticket(
    *,
    factory: Callable[[], "TicketService | None"],
    tenant_id: str,
    conversation_id: str,
    content_text: str,
) -> None:
    """Best-effort ticket auto-create hook invoked from ``record_message``.

    Pulled out of ``ConversationService.record_message`` so the
    customer-inbound hot path stays readable. The factory is invoked
    lazily so tests that mock ``ConversationService`` never construct
    a real TicketService at kwargs time.

    All exceptions are caught and logged at WARNING with opaque IDs
    only — a ticket-creation failure must NOT fail the customer
    message ingest (the customer's turn is the product).
    """
    try:
        ticket_svc = factory()
    except Exception as exc:
        logger.warning(
            "conversation ticket factory construction failed",
            extra={
                "conversation_id": conversation_id,
                "tenant_id": tenant_id,
                "error_type": type(exc).__name__,
            },
        )
        return
    if ticket_svc is None:
        return
    try:
        existing = await ticket_svc.repo.get_for_conversation(
            conversation_id, tenant_id=tenant_id
        )
        if existing is not None:
            return
        # ``subject`` is the first 120 chars of the customer message —
        # PII; we pass it to ``TicketService.create`` only and never
        # log it. The existing ``ticket_created`` log line in
        # ``TicketService.create`` carries opaque IDs only.
        subject = (content_text or "Customer inquiry")[:120]
        await ticket_svc.create(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            subject=subject,
        )
    except Exception as exc:
        logger.warning(
            "conversation ticket auto-create failed",
            extra={
                "conversation_id": conversation_id,
                "tenant_id": tenant_id,
                "error_type": type(exc).__name__,
            },
        )

# Default and hard-cap page sizes used by the service layer for tenant /
# message listing. The repositories' own defaults may differ (see the
# list_by_conversation default of 100) — that's intentional: the repo
# serves both this service and the tests, while the service default
# reflects the API contract for the inbox/message list endpoints.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class ConversationService:
    """Tenant-scoped service for Conversation + Message rows."""

    def __init__(
        self,
        repo: ConversationRepository | None = None,
        message_repo: MessageRepository | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        ticket_service_factory: Callable[[], "TicketService | None"] | None = None,
    ) -> None:
        self._repo = repo or ConversationRepository()
        self._message_repo = message_repo or MessageRepository()
        self._clock = clock or (lambda: datetime.now(UTC))
        # Optional auto-create hook for Tickets (Task 6, M2.A). Set
        # to a callable that returns a ``TicketService`` (or None to
        # opt out). The callable is invoked LAZILY inside
        # ``record_message`` so tests that mock ``ConversationService``
        # don't pay the cost of constructing one — and so we don't
        # require a live DB to instantiate a service that might never
        # be used. ``channel.inbound`` wires this up; agent-reply /
        # escalation paths leave it None.
        self._ticket_service_factory = ticket_service_factory

    # ---- Inbound / lookup ----

    async def find_or_create_for_inbound(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        customer_external_id: str,
    ) -> Conversation | None:
        """Return the open conversation for (channel, customer), or create one.

        The caller (channel adapter) is expected to have already validated
        that the channel belongs to ``tenant_id``. We touch
        ``last_activity_at`` on a hit because the customer just sent
        another message.

        Defence-in-depth: if an existing open conversation row happens to
        belong to a different tenant (which should be unreachable since
        channel_id is a globally-unique ULID), we return ``None`` rather
        than touch it or surface it to the caller. Returns ``None`` in
        that cross-tenant case only.
        """
        existing = await self._repo.find_open_by_channel_customer(
            channel_id=channel_id,
            customer_external_id=customer_external_id,
        )
        if existing is not None:
            if existing.tenant_id != tenant_id:
                logger.warning(
                    "conversation.cross_tenant_probe_blocked",
                    extra={
                        "channel_id": channel_id,
                        "tenant_id": tenant_id,
                    },
                )
                return None
            await self._repo.touch_last_activity(
                conversation_id=existing.id, at=self._clock()
            )
            return existing

        now = self._clock()
        conv = Conversation(
            id=new_id(),
            tenant_id=tenant_id,
            channel_id=channel_id,
            customer_external_id=customer_external_id,
            status=ConversationStatus.OPEN,
            assigned_agent_id=None,
            ai_handling=True,
            opened_at=now,
            last_activity_at=now,
        )
        try:
            return await self._repo.create(conversation=conv)
        except IntegrityError:
            # Race: another concurrent inbound created the open conversation
            # between our find and our insert. The partial unique index
            # uq_conversations_channel_customer_open caught it. Re-read to
            # return the winner if it belongs to us.
            existing = await self._repo.find_open_by_channel_customer(
                channel_id=channel_id,
                customer_external_id=customer_external_id,
            )
            if existing is not None and existing.tenant_id == tenant_id:
                logger.info(
                    "conversation.race_resolved_by_unique_index",
                    extra={
                        "channel_id": channel_id,
                        "tenant_id": tenant_id,
                    },
                )
                await self._repo.touch_last_activity(
                    conversation_id=existing.id, at=self._clock()
                )
                return existing
            # The winning row belongs to another tenant — this is the
            # legitimate cross-tenant-abuse signal; re-raise.
            raise

    async def list_for_tenant(
        self,
        *,
        tenant_id: str,
        status: ConversationStatus | None = None,
        search: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[Conversation]:
        """List conversations for a tenant, newest-activity first."""
        return await self._repo.list_by_tenant(
            tenant_id=tenant_id,
            status=status,
            search=search,
            limit=limit,
            offset=offset,
        )

    async def list_for_agent(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        status: ConversationStatus | None = None,
        search: str | None = None,
    ) -> list[Conversation]:
        """List conversations assigned to ``agent_id`` within ``tenant_id``.

        Defence-in-depth: even if the repository is ever extended to
        return cross-tenant rows (e.g. an agent id collision across
        tenants), this layer strips out anything that doesn't belong to
        the requesting tenant.
        """
        rows = await self._repo.list_by_assigned_agent(
            assigned_agent_id=agent_id,
            status=status,
            search=search,
        )
        return [r for r in rows if r.tenant_id == tenant_id]

    async def get(
        self, *, tenant_id: str, conversation_id: str
    ) -> Conversation | None:
        """Return the conversation if it belongs to the tenant; otherwise None.

        Returning ``None`` for both "not found" and "wrong tenant" prevents
        tenant enumeration via response timing/status differences — the
        API layer maps both cases to 404.
        """
        conv = await self._repo.get_by_id(conversation_id)
        if conv is None:
            return None
        if conv.tenant_id != tenant_id:
            logger.warning(
                "conversation.cross_tenant_probe_blocked",
                extra={
                    "conversation_id": conversation_id,
                    "tenant_id": tenant_id,
                },
            )
            return None
        return conv

    # ---- State transitions ----

    async def assign_to_agent(
        self, *, tenant_id: str, conversation_id: str, agent_id: str
    ) -> Conversation | None:
        """Move the conversation to PENDING with ``agent_id`` assigned.

        Returns None on cross-tenant / missing. Updates
        ``last_activity_at`` so the conversation sorts to the top of the
        agent's queue.
        """
        conv = await self.get(
            tenant_id=tenant_id, conversation_id=conversation_id
        )
        if conv is None:
            return None
        conv.status = ConversationStatus.PENDING
        conv.assigned_agent_id = agent_id
        conv.ai_handling = False
        conv.last_activity_at = self._clock()
        updated = await self._repo.update(conv)
        if updated is not None:
            logger.info(
                "conversation assigned to agent",
                extra={
                    "conversation_id": conversation_id,
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                },
            )
        return updated

    async def claim(
        self, *, tenant_id: str, conversation_id: str, agent_id: str
    ) -> Conversation:
        """Atomically claim a PENDING conversation for ``agent_id``.

        This is the agent workspace's "take ownership" action
        (Stage 8.2). Wraps ``SELECT ... FOR UPDATE`` + status check
        + write in a single transaction so two concurrent claim
        attempts on the same row cannot both succeed:

        1. Acquire a row lock on the conversation via
           :meth:`ConversationRepository.get_by_id_for_update`.
        2. Re-read the row state under the lock. If the row doesn't
           exist (or belongs to another tenant) → raise
           ``ValueError`` (mapped to 404 by the API).
        3. If the row's ``status != PENDING`` or
           ``assigned_agent_id IS NOT NULL`` → raise
           :class:`ConversationNotClaimableError` (mapped to 409 by
           the API). The lock is released by the rollback before
           the exception propagates.
        4. Otherwise set ``assigned_agent_id = agent_id``, keep
           ``status = PENDING``, keep ``ai_handling = False``,
           advance ``last_activity_at`` via the injected clock, and
           commit.

        ``status`` is NOT changed by claim — the conversation stays
        PENDING while the agent owns it. The agent (or an admin)
        flips the conversation back to AI handling via the existing
        ``return_to_ai`` action; closing stays on the
        ``close`` action.

        Anti-enumeration: the not-found branch raises ``ValueError``
        with the same wording as :meth:`record_message` so the API
        layer can map both to 404. The "already claimed / wrong
        status" branch raises a single
        :class:`ConversationNotClaimableError` so a probing caller
        cannot distinguish between the two failure modes via the
        response body — both surface as 409 with the same
        ``"conversation cannot be claimed"`` message.

        Raises
        ------
        ValueError
            Cross-tenant or unknown ``conversation_id``. Mapped to
            404 by the API layer.
        ConversationNotClaimableError
            Already claimed by another agent, OR the conversation
            is in a non-PENDING state. Mapped to 409 by the API
            layer.
        """
        sm = get_sessionmaker()
        async with sm() as session:
            conv = await self._repo.get_by_id_for_update(
                session=session,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )
            if conv is None:
                # Cross-tenant or unknown — same 404 the rest of
                # the API uses to prevent enumeration via response
                # differentiation. Roll back so we don't pin an
                # empty transaction on the connection pool.
                await session.rollback()
                raise ValueError(
                    f"conversation {conversation_id} not found for tenant {tenant_id}"
                )
            if (
                conv.status != ConversationStatus.PENDING
                or conv.assigned_agent_id is not None
            ):
                # Anti-enumeration: one error class covers both
                # "already claimed" and "wrong status". The agent
                # had to know the conversation_id to attempt the
                # claim, so a 409 here doesn't reveal anything they
                # couldn't have inferred from the URL.
                await session.rollback()
                raise ConversationNotClaimableError()

            now = self._clock()
            conv.assigned_agent_id = agent_id
            # status stays PENDING — claim does NOT advance to a
            # new state. ai_handling stays False (it's already
            # False for any PENDING conversation; this is a no-op
            # but documents intent).
            conv.ai_handling = False
            conv.last_activity_at = now
            await session.flush()
            await session.refresh(conv)
            await session.commit()

        logger.info(
            "conversation claimed by agent",
            extra={
                "conversation_id": conversation_id,
                "tenant_id": tenant_id,
                "agent_id": agent_id,
            },
        )
        return conv

    async def escalate_to_human_queue(
        self, *, tenant_id: str, conversation_id: str
    ) -> Conversation | None:
        """Move the conversation to PENDING with no agent assigned (queue wait).

        Distinct from :meth:`assign_to_agent` which targets a specific
        agent — this method is the escalation entry point used by the
        AI auto-reply's ``escalate_to_human`` tool. The conversation
        sits in the human queue waiting to be claimed by an agent.

        Returns ``None`` on cross-tenant / missing. Updates
        ``last_activity_at`` so the conversation sorts to the top of
        the queue. Tenant isolation is re-validated by the inner
        ``self.get(...)`` call — no caller can smuggle a foreign
        ``conversation_id`` past the service boundary.
        """
        conv = await self.get(
            tenant_id=tenant_id, conversation_id=conversation_id
        )
        if conv is None:
            return None
        conv.status = ConversationStatus.PENDING
        conv.assigned_agent_id = None
        conv.ai_handling = False
        conv.last_activity_at = self._clock()
        updated = await self._repo.update(conv)
        if updated is not None:
            logger.info(
                "conversation escalated to human queue",
                extra={
                    "conversation_id": conversation_id,
                    "tenant_id": tenant_id,
                },
            )
        return updated

    async def return_to_ai(
        self, *, tenant_id: str, conversation_id: str
    ) -> Conversation | None:
        """Return the conversation to AI handling (OPEN, no agent, ai_handling=True)."""
        conv = await self.get(
            tenant_id=tenant_id, conversation_id=conversation_id
        )
        if conv is None:
            return None
        conv.status = ConversationStatus.OPEN
        conv.assigned_agent_id = None
        conv.ai_handling = True
        conv.last_activity_at = self._clock()
        updated = await self._repo.update(conv)
        if updated is not None:
            logger.info(
                "conversation returned to ai",
                extra={
                    "conversation_id": conversation_id,
                    "tenant_id": tenant_id,
                },
            )
        return updated

    async def close(
        self, *, tenant_id: str, conversation_id: str
    ) -> Conversation | None:
        """Close the conversation. ``assigned_agent_id`` is preserved."""
        conv = await self.get(
            tenant_id=tenant_id, conversation_id=conversation_id
        )
        if conv is None:
            return None
        conv.status = ConversationStatus.CLOSED
        conv.ai_handling = False
        conv.last_activity_at = self._clock()
        updated = await self._repo.update(conv)
        if updated is not None:
            logger.info(
                "conversation closed",
                extra={
                    "conversation_id": conversation_id,
                    "tenant_id": tenant_id,
                },
            )
        return updated

    # ---- Messages ----

    async def record_message(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        role: MessageRole,
        content_text: str,
        sender_id: str | None = None,
        content_blocks: dict[str, Any] | None = None,
        tool_calls: dict[str, Any] | None = None,
    ) -> Message:
        """Persist a Message row and advance the conversation's activity.

        Raises ``ValueError`` if the conversation does not belong to the
        tenant — fail-loud at the boundary so a misconfigured caller is
        caught immediately rather than silently writing cross-tenant
        data.
        """
        conv = await self.get(
            tenant_id=tenant_id, conversation_id=conversation_id
        )
        if conv is None:
            raise ValueError(
                f"conversation {conversation_id} not found for tenant {tenant_id}"
            )

        now = self._clock()
        msg = Message(
            id=new_id(),
            conversation_id=conversation_id,
            role=role,
            content_text=content_text,
            sender_id=sender_id,
            content_blocks_json=content_blocks,
            tool_calls_json=tool_calls,
            created_at=now,
        )
        persisted = await self._message_repo.create(message=msg)
        await self._repo.touch_last_activity(
            conversation_id=conversation_id, at=now
        )
        # Stage 11.3: bump the per-role message counter so the
        # ``/metrics`` endpoint surfaces aggregate conversation throughput.
        # ``role`` is the StrEnum value (5 fixed labels), so this stays
        # well within Prometheus cardinality budget. We do NOT label by
        # tenant_id — that's billing territory, not metric territory.
        MESSAGES_TOTAL.labels(role=role.value).inc()

        # Task 6 (M2.A): auto-create a Ticket on the first CUSTOMER
        # message in a conversation. Only the customer-inbound path
        # wires ``_ticket_service_factory``; agent-reply and
        # escalation paths leave it None and this branch is a no-op.
        #
        # The factory is invoked lazily so tests that mock
        # ``ConversationService`` never construct a TicketService at
        # kwargs-evaluation time (which would force a DB connection
        # during the test's monkeypatch setup).
        #
        # ``subject`` is the first 120 chars of the customer message —
        # that's PII. We pass it to ``TicketService.create`` only;
        # NEVER log it (the existing ``ticket_created`` log line in
        # ``TicketService.create`` carries opaque IDs only).
        if (
            role == MessageRole.CUSTOMER
            and self._ticket_service_factory is not None
        ):
            await _try_auto_create_ticket(
                factory=self._ticket_service_factory,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                content_text=content_text,
            )

        return persisted

    async def list_messages(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        before: datetime | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> list[Message] | None:
        """Return messages for a tenant-owned conversation, oldest-first.

        Returns ``None`` if the conversation is missing or belongs to a
        different tenant.
        """
        conv = await self.get(
            tenant_id=tenant_id, conversation_id=conversation_id
        )
        if conv is None:
            return None
        return await self._message_repo.list_by_conversation(
            conversation_id=conversation_id,
            before=before,
            limit=limit,
        )
