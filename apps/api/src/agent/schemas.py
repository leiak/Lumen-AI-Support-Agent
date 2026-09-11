"""Pydantic schemas for the agent workspace API (Stage 8).

These models define the public request/response shapes for the
``/api/v1/agents`` surface — used by the agent workspace frontend for
``GET /me`` (top-bar identity) and ``POST /conversations/{id}/messages``
(reply box).
"""
from __future__ import annotations

from pydantic import BaseModel, Field


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
