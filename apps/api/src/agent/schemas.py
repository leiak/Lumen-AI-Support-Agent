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

from pydantic import BaseModel, Field

from conversation.enums import ConversationStatus


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
