"""Agent workspace HTTP API (Stage 8.1 + 8.2 + 8.3).

This router owns the agent-facing endpoints used by the workspace
frontend:

- ``GET /api/v1/agents/me`` — return the JWT-derived caller identity,
  augmented with the tenant's display name from ``TenantRepository``.
- ``GET /api/v1/agents/queue`` — list PENDING conversations in the
  caller's tenant whose ``assigned_agent_id IS NULL`` (Stage 8.2).
- ``POST /api/v1/agents/conversations/{conversation_id}/claim`` —
  atomic claim: the calling agent takes ownership of a PENDING
  conversation. Uses ``SELECT ... FOR UPDATE`` so two concurrent
  claim attempts cannot both succeed (Stage 8.2).
- ``POST /api/v1/agents/conversations/{conversation_id}/suggest-reply``
  — read-only AI-suggested reply preview for the workspace UI
  (Stage 8.3). Returns suggested text, the retrieved RAG chunks
  formatted as citations, and a ``turn_kind`` discriminator.

The agent reply endpoint (``POST /conversations/{id}/messages``) lives
on the conversation router to keep its canonical URL
``/api/v1/conversations/{id}/messages`` and so the existing
anti-enumeration + tenant-isolation story stays in one place.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from agent.exceptions import SuggestionServiceNotFoundError
from agent.schemas import (
    AgentMeOut,
    ConversationListOut,
    ConversationOut,
    SuggestionOut,
)
from agent.suggest import SuggestionService
from auth.dependencies import require_agent_or_admin
from conversation.enums import ConversationStatus
from conversation.exceptions import ConversationNotClaimableError
from conversation.models import Conversation
from conversation.repository import ConversationRepository
from conversation.service import ConversationService
from core.logging import get_logger
from tenant.repository import TenantRepository

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

log = get_logger(__name__)


# ---- Mappers ------------------------------------------------------------


def _conv_out(c: Conversation) -> ConversationOut:
    """Map a Conversation ORM row to the public workspace shape.

    Defined locally (vs. imported from ``conversation.api``) so the
    two API surfaces — agent workspace vs. conversation admin —
    can diverge their response shape independently. Today the
    fields are identical; tomorrow the workspace may want to drop
    ``customer_external_id`` from queue responses for the agent UI.
    """
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


# ---- Routes -------------------------------------------------------------


@router.get("/me", response_model=AgentMeOut)
async def get_me(
    claims: Annotated[dict[str, Any], Depends(require_agent_or_admin)],
) -> AgentMeOut:
    """Return the caller's identity (JWT-derived).

    The JWT carries ``sub`` (user_id), ``tenant_id``, ``role`` and
    ``email`` (when issued via the auth service). The human-readable
    ``tenant_name`` is looked up by id from ``TenantRepository`` and
    falls back to the tenant id if the lookup fails — never leaks an
    error to the caller (anti-enumeration parity with the rest of
    the workspace API).
    """
    tenant_id = claims["tenant_id"]
    user_id = claims["sub"]
    role = claims.get("role", "agent")
    email = str(claims.get("email", ""))
    tenant = await TenantRepository().get_by_id(tenant_id)
    tenant_name = tenant.name if tenant is not None else tenant_id
    # PII-safe log per Stage 8.1 spec: only user_id + tenant_id.
    # `role` is on the response body but intentionally NOT logged.
    log.info(
        "agent identity fetched",
        user_id=user_id,
        tenant_id=tenant_id,
    )
    return AgentMeOut(
        user_id=user_id,
        email=email,
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        role=role,
    )


@router.get("/queue", response_model=ConversationListOut)
async def get_queue(
    claims: Annotated[dict[str, Any], Depends(require_agent_or_admin)],
    status_filter: Annotated[
        ConversationStatus | None,
        Query(
            alias="status",
            description=(
                "Conversation status to filter by. Defaults to PENDING "
                "for the unassigned-queue use case; admins can pass "
                "OPEN/CLOSED to see all rows with that status."
            ),
        ),
    ] = ConversationStatus.PENDING,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConversationListOut:
    """List the tenant's unassigned PENDING conversations (Stage 8.2 queue).

    The agent workspace's "work waiting for someone to take it" view.
    Sorted by ``last_activity_at DESC`` so the freshest activity
    surfaces first.

    Auth: ``require_agent_or_admin`` — agents see the queue; admins
    also see it (and can override the status filter below).

    Status filter semantics:

    * ``status=PENDING`` (default) → PENDING + ``assigned_agent_id
      IS NULL`` — i.e. the unassigned queue.
    * ``status=OPEN`` / ``status=CLOSED`` → ALL conversations in
      that status regardless of assignment. Admins use this for
      reporting; agents who pass a non-PENDING value get the same
      unfiltered view (the repo path treats all non-PENDING
      statuses uniformly — there is no separate "admin override"
      gate).

    PII-safe log payload: only ``user_id``, ``tenant_id``, the
    filtered ``status``, and the row count. No customer text, no
    agent id beyond the caller's own.
    """
    effective_status = (
        status_filter if status_filter is not None else ConversationStatus.PENDING
    )
    items = await ConversationRepository().list_pending_for_tenant(
        tenant_id=claims["tenant_id"],
        status=effective_status,
        limit=limit,
        offset=offset,
    )
    log.info(
        "agent queue fetched",
        tenant_id=claims["tenant_id"],
        user_id=claims["sub"],
        status=str(effective_status),
        item_count=len(items),
        limit=limit,
        offset=offset,
    )
    return ConversationListOut(items=[_conv_out(c) for c in items])


@router.post(
    "/conversations/{conversation_id}/claim",
    response_model=ConversationOut,
)
async def claim_conversation(
    conversation_id: str,
    claims: Annotated[dict[str, Any], Depends(require_agent_or_admin)],
) -> ConversationOut:
    """Atomically claim a PENDING conversation for the calling agent.

    Stage 8.2: completes the agent's queue/claim workflow by pairing
    ``GET /queue`` (find work) with ``POST /claim`` (take ownership).

    Semantics:

    * Sets ``assigned_agent_id = claims["sub"]`` — the calling
      agent's opaque JWT identifier, safe to log.
    * Keeps ``status = PENDING`` — claim does NOT advance state. The
      conversation stays PENDING while the agent owns it. Closing
      remains on the ``/close`` endpoint; handing back to AI
      remains on ``/return-to-ai``.
    * Keeps ``ai_handling = False`` — already False for any PENDING
      conversation; this is a documentation no-op so a future
      reader doesn't wonder.
    * Advances ``last_activity_at`` via the service's injected clock
      so the claimed conversation sorts to the top of the agent's
      inbox.

    Atomicity: implemented inside ``ConversationService.claim`` via
    ``SELECT ... FOR UPDATE`` + status check + write in a single
    transaction. Two concurrent claim attempts on the same row
    serialize at the DB level; the second sees the freshly-updated
    ``assigned_agent_id`` and surfaces the same
    ``ConversationNotClaimableError`` (→ 409) the wrong-status
    branch produces. Anti-enumeration: the 409 message is identical
    for both failure modes.

    No WS broadcast: claim is a state change, not a message event.
    Subscribers learn about the new owner by refetching the
    conversation via REST, not via a WS frame.

    Error mapping:

    * Cross-tenant / unknown ``conversation_id`` → 404 (anti-
      enumeration: same wording as ``POST /conversations/{id}/messages``).
    * ``status != PENDING`` OR ``assigned_agent_id IS NOT NULL``
      → 409 with the generic
      ``"conversation cannot be claimed"`` message. The two
      failure modes are deliberately indistinguishable so a probing
      caller cannot enumerate state across the tenant boundary.
    """
    tenant_id = claims["tenant_id"]
    agent_id = claims["sub"]
    try:
        conv = await ConversationService().claim(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
        )
    except ValueError:
        # Cross-tenant or unknown — same 404 the rest of the API
        # uses to prevent enumeration via response differentiation.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="conversation not found",
        ) from None
    except ConversationNotClaimableError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="conversation cannot be claimed",
        ) from None
    log.info(
        "agent claimed conversation",
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
    )
    return _conv_out(conv)


@router.post(
    "/conversations/{conversation_id}/suggest-reply",
    response_model=SuggestionOut,
)
async def suggest_reply(
    conversation_id: str,
    claims: Annotated[dict[str, Any], Depends(require_agent_or_admin)],
) -> SuggestionOut:
    """Generate a read-only AI-suggested reply preview (Stage 8.3).

    Given the conversation's current state (last customer message
    + recent history), returns:

    * ``suggested_text`` — the LLM-generated reply the agent
      could send. If the LLM call fails, falls back to the
      standard ``FALLBACK_MESSAGE`` and surfaces
      ``turn_kind="llm_unavailable"`` + ``warning="llm_unavailable"``.
    * ``citations`` — the retrieved RAG chunks used as context,
      formatted as ``(article_id, chunk_index, text, score)``
      tuples so the frontend can render ``"(article {id},
      chunk {idx})"`` links.
    * ``retrieval_score_max`` — highest cosine similarity across
      the retrieved chunks (``0.0`` when no chunks).
    * ``turn_kind`` — one of ``rag_hit`` / ``no_rag`` /
      ``no_customer_message`` / ``llm_unavailable``.
    * ``warning`` — currently only set to ``"llm_unavailable"``
      when the LLM failed; ``None`` on success.

    **Critical invariants (pinned by tests):**

    * **READ-ONLY.** The endpoint NEVER persists anything to the
      DB and NEVER mutates conversation state. It does not call
      :meth:`ConversationService.record_message`, does not
      dispatch the ``escalate_to_human`` tool, and does not
      broadcast WS events.
    * **PII-safe logs.** Only opaque IDs (``tenant_id``,
      ``conversation_id``) plus ``error_type`` on failure.
    * **Tenant isolation.** Cross-tenant or unknown
      ``conversation_id`` → 404 with the generic
      ``"conversation not found"`` wording the rest of the
      workspace API uses (anti-enumeration).
    * **RAG fail-open.** ``EmbeddingError`` or any other RAG
      failure → empty citations, ``turn_kind="no_rag"``, the LLM
      call still proceeds.
    * **LLM fail-safe.** ``RateLimited`` /
      ``ProviderUnavailable`` / ``OutputInvalid`` /
      ``InvalidRequest`` → ``suggested_text=FALLBACK_MESSAGE``,
      ``warning="llm_unavailable"``,
      ``turn_kind="llm_unavailable"``.

    Auth: ``require_agent_or_admin`` so assigned agents and
    admins can both request a preview. The request body is
    currently empty — future extensions (model override,
    ``max_tokens`` override, etc.) will be additive and won't
    change the auth contract.
    """
    tenant_id = claims["tenant_id"]
    try:
        result = await SuggestionService().suggest_reply(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )
    except SuggestionServiceNotFoundError:
        # Cross-tenant or unknown — same 404 the rest of the API
        # uses to prevent enumeration via response differentiation.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="conversation not found",
        ) from None
    log.info(
        "agent suggestion generated",
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        turn_kind=result.turn_kind,
        citation_count=len(result.citations),
    )
    return SuggestionOut(
        conversation_id=result.conversation_id,
        suggested_text=result.suggested_text,
        citations=result.citations,
        retrieval_score_max=result.retrieval_score_max,
        warning=result.warning,
        turn_kind=result.turn_kind,  # type: ignore[arg-type]
    )