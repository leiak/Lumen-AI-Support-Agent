"""Pydantic schemas for the agent workspace API (Stage 8).

These models define the public request/response shapes for the
``/api/v1/agents`` surface — used by the agent workspace frontend for
``GET /me`` (top-bar identity), ``POST /conversations/{id}/messages``
(reply box), ``GET /queue`` (work waiting to be claimed), and
``POST /conversations/{id}/claim`` (atomic claim).

Stage 8.2 note
--------------

``ConversationOut`` and ``ConversationListOut`` are duplicated here
rather than imported from ``conversation.api``. The
``conversation.api`` module is the wrong import source (it would
create a cycle: ``agent.api`` already needs
``conversation.service`` for the claim endpoint, and pulling
``ConversationListOut`` through ``conversation.api`` would drag in
the rest of the conversation router as a transitive dep). Keeping
the schemas here also lets us evolve the agent-workspace response
shape (e.g. trim fields not used by the queue view) without
disturbing the conversation API contract.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from conversation.enums import ConversationStatus

# ---------------------------------------------------------------------------
# Stage 8.3 — AI-suggested reply shapes
# ---------------------------------------------------------------------------

# Truncation limit for ``CitationOut.text``. The full chunk text
# lives in the chunks table and could be very long (KB articles
# can run thousands of characters per chunk); the frontend only
# needs enough to render a tooltip / preview. 200 chars keeps the
# suggestion payload compact while still showing the chunk's
# gist for the agent to verify the citation.
CITATION_TEXT_MAX_CHARS = 200


class AgentMeOut(BaseModel):
    """Public representation of the calling agent's identity.

    Sourced from the JWT claims (``sub`` → ``user_id``, ``email``,
    ``tenant_id``, ``role``) plus a single ``TenantRepository`` lookup
    for the human-readable ``tenant_name``. Never returns password hashes
    or other internal-only fields.
    """

    user_id: str
    email: str
    tenant_id: str
    tenant_name: str
    role: str


class AgentMessageCreate(BaseModel):
    """POST /conversations/{id}/messages body — agent reply payload.

    Length bounds mirror the customer-inbound bound for parity
    (1-4000 chars). Whitespace is stripped at the API boundary via the
    ``stripped_text`` helper — an all-whitespace payload that passes
    Pydantic's ``min_length`` check is rejected by the route handler
    with 422.
    """

    content_text: str = Field(min_length=1, max_length=4000)

    @property
    def stripped_text(self) -> str:
        """Return ``content_text`` with surrounding whitespace removed."""
        return self.content_text.strip()


# ---------------------------------------------------------------------------
# Stage 8.2 — queue + claim response shapes
# ---------------------------------------------------------------------------


class ConversationOut(BaseModel):
    """Public conversation representation used by the agent workspace.

    Mirrors ``conversation.api.ConversationOut`` — kept local to avoid
    a circular import through ``conversation.api``. Field names match
    the conversation API's existing public shape so the frontend can
    reuse the same TypeScript type for inbox / queue / claim responses.
    """

    id: str
    tenant_id: str
    channel_id: str
    customer_external_id: str
    status: ConversationStatus
    assigned_agent_id: str | None
    ai_handling: bool
    opened_at: datetime
    last_activity_at: datetime


class ConversationListOut(BaseModel):
    """Paginated list wrapper for the queue endpoint.

    No total — list endpoints don't run a separate count query (matches
    the existing ``conversation.api.ConversationListOut`` shape).
    """

    items: list[ConversationOut]


# ---------------------------------------------------------------------------
# Stage 8.3 — AI-suggested reply response shapes
# ---------------------------------------------------------------------------


class CitationOut(BaseModel):
    """One RAG citation surfaced to the agent workspace UI.

    Represents a single retrieved chunk the LLM saw when producing
    the suggestion. The frontend uses ``article_id`` +
    ``chunk_index`` to render an ``"(article {id}, chunk {idx})"``
    link so the agent can verify the citation by jumping to the
    source article.

    ``text`` is truncated to :data:`CITATION_TEXT_MAX_CHARS` to keep
    the suggestion payload compact; the full chunk text is
    available via the KB admin API when the agent needs more
    context.

    PII: ``article_id`` and ``chunk_index`` are opaque ULIDs /
    small integers — safe to surface in the response. ``text`` is
    KB content (already curated by the tenant); it is NOT
    customer PII, but we still bound its size to keep payloads
    predictable.
    """

    article_id: str
    chunk_index: int
    text: str
    score: float


# Literal alias for the ``turn_kind`` discriminator on
# ``SuggestionOut``. Centralised here so the route handler and
# tests share a single source of truth.
SuggestionTurnKind = Literal["rag_hit", "no_rag", "no_customer_message", "llm_unavailable"]


class SuggestionOut(BaseModel):
    """Response shape for ``POST /conversations/{id}/suggest-reply``.

    Read-only "show me what the AI would say right now" preview
    for the agent workspace. The endpoint NEVER persists anything
    to the DB and NEVER mutates conversation state — every
    field on this response is a snapshot.

    ``turn_kind`` is the discriminator the frontend uses to drive
    empty-state UI ("no KB yet" vs. "no customer message" vs.
    "AI unavailable"):

    * ``rag_hit`` — RAG returned ≥1 chunk and the LLM produced a
      reply. ``citations`` is populated; ``retrieval_score_max > 0``.
    * ``no_rag`` — tenant has no KB OR RAG failed (fail-open).
      ``citations`` is empty; ``suggested_text`` is still populated
      from the LLM's unaugmented reply.
    * ``no_customer_message`` — the conversation has no customer
      turn to anchor a suggestion on. ``suggested_text`` is empty;
      ``citations`` is empty; ``warning`` is ``None``.
    * ``llm_unavailable`` — the LLM call failed (rate-limit,
      provider down, etc.). ``suggested_text`` is
      :data:`agent.graph.prompts.FALLBACK_MESSAGE`; ``warning``
      carries ``"llm_unavailable"`` so the frontend can render a
      retry button without parsing free-form error text.
    """

    conversation_id: str
    suggested_text: str
    citations: list[CitationOut]
    retrieval_score_max: float
    warning: str | None = None
    turn_kind: SuggestionTurnKind
