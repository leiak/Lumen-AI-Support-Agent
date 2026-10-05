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


__all__ = ["TenantLLMConfigCreate", "TenantLLMConfigRead"]