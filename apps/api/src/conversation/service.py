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
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation, Message
from conversation.repository import ConversationRepository, MessageRepository
from core.id_gen import new_id

logger = logging.getLogger(__name__)

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
    ) -> None:
        self._repo = repo or ConversationRepository()
        self._message_repo = message_repo or MessageRepository()
        self._clock = clock or (lambda: datetime.now(UTC))

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
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[Conversation]:
        """List conversations for a tenant, newest-activity first."""
        return await self._repo.list_by_tenant(
            tenant_id=tenant_id,
            status=status,
            limit=limit,
            offset=offset,
        )

    async def list_for_agent(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        status: ConversationStatus | None = None,
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
