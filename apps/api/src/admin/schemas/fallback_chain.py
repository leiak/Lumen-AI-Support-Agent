"""Pydantic schemas for tenant fallback chain admin endpoints.

M5.1 — per-tenant LLM fallback chain override.

PII discipline: response carries tenant_id + chain steps + timestamps.
No API keys are stored on the chain (the chain references the
provider; the key lives on ``tenant_llm_configs`` per M4.C).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ChainStepIn(BaseModel):
    """One step in the requested chain."""

    provider: Literal["minimax", "anthropic", "openai"] = Field(
        ..., description="LLM provider identifier for this step",
    )
    model: str = Field(
        ..., min_length=1, max_length=128,
        description="Model name to pin for this step",
    )
    enabled: bool = Field(default=True)


class TenantFallbackChainUpsert(BaseModel):
    """Request body for POST /admin/tenants/{tenant_id}/fallback-chain."""

    steps: list[ChainStepIn] = Field(..., min_length=1)
    enabled: bool = Field(default=True)

    @field_validator("steps")
    @classmethod
    def _at_least_one_enabled(cls, v: list[ChainStepIn]) -> list[ChainStepIn]:
        if not any(s.enabled for s in v):
            raise ValueError(
                "chain must contain at least one enabled step"
            )
        return v


class ChainStepOut(BaseModel):
    """One step in the returned chain (mirrors ChainStepIn but without Pydantic validators)."""

    provider: str
    model: str
    enabled: bool


class TenantFallbackChainOut(BaseModel):
    """Response body for GET/POST /admin/tenants/{tenant_id}/fallback-chain."""

    tenant_id: str
    steps: list[ChainStepOut]
    enabled: bool
    created_at: datetime
    updated_at: datetime


__all__ = [
    "ChainStepIn",
    "ChainStepOut",
    "TenantFallbackChainOut",
    "TenantFallbackChainUpsert",
]