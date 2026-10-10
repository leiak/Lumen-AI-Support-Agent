"""Pydantic schemas for tenant LLM config admin endpoints.

M4.C Task 4 — admin BYOK registration + listing.

PII discipline
--------------

The ``TenantLLMConfigRead`` response schema deliberately omits BOTH
``api_key`` (plaintext) AND ``encrypted_api_key`` (ciphertext bytes).
Even though the ciphertext is "safe" in isolation (Fernet is
authenticated encryption), leaking it lets an attacker who already
has the master key (e.g. via a stolen env var) decrypt every row.
The admin UI lists provider names + timestamps only; operators who
want to inspect ciphertext MUST read the DB row directly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TenantLLMConfigCreate(BaseModel):
    """Request body for POST /admin/tenants/{tenant_id}/llm-configs."""

    provider_name: Literal["minimax", "anthropic", "openai"] = Field(
        ..., description="LLM provider identifier",
    )
    api_key: str = Field(
        ..., min_length=1, max_length=512,
        description="Plaintext API key (encrypted at rest with Fernet)",
    )
    base_url: str | None = Field(
        default=None, max_length=512,
        description="Optional override for provider base URL",
    )
    enabled: bool = Field(default=True)


class TenantLLMConfigRead(BaseModel):
    """Response body — NEVER includes the API key (plaintext or ciphertext)."""

    provider_name: str
    base_url: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime


class TenantLLMConfigUpdate(BaseModel):
    """Request body for PATCH /admin/tenants/{tenant_id}/llm-configs/{provider_name}.

    Tier 1 Task 1.3: lets the admin SPA toggle ``enabled`` and adjust
    ``base_url`` WITHOUT requiring the operator to re-submit the API
    key. The encrypted key is preserved across the update — only the
    mutable sidecar fields change. Leaving a field out (or sending
    ``None`` for ``base_url``) is a no-op for that field.
    """

    enabled: bool | None = Field(
        default=None,
        description="Toggle on/off. Omit to keep current value.",
    )
    base_url: str | None = Field(
        default=None, max_length=512,
        description="Override for provider base URL. Pass an empty string "
        "to clear. Omit to keep current value.",
    )


__all__ = ["TenantLLMConfigCreate", "TenantLLMConfigRead", "TenantLLMConfigUpdate"]