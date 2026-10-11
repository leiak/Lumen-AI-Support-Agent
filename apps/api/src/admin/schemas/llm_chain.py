"""Runtime LLM provider chain admin schemas (Tier 3 Task 3.4).

The chain is a process-level singleton (initialised from
``LLM_FALLBACK_CHAIN`` env, then mutated by the admin endpoints).
This shape is intentionally minimal — three string fields per step
— because the SPA renders a reorderable drag list and a per-row
disable toggle.

PII discipline
--------------

The chain carries provider names + model identifiers. API keys are
NEVER echoed back; they're owned by ``BaseProvider`` and read by
the gateway at call time. The admin sees which provider+model is
active, not the credentials backing it.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class LLMChainStep(BaseModel):
    """One row in the runtime chain.

    ``enabled`` lets the operator temporarily skip a step without
    losing its position (so re-enabling keeps its place in the
    order). The chain is the *runtime* chain — empty / fully
    disabled chains are rejected (see admin endpoint).
    """

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    enabled: bool = True


class LLMProviderInfo(BaseModel):
    """One registered provider's metadata (no credentials)."""

    name: str
    model: str
    is_default: bool


class LLMChainRead(BaseModel):
    """Current chain + the provider registry snapshot.

    Both fields are returned together so the SPA can render the
    dropdown of "available providers" without a second round-trip.
    """

    steps: list[LLMChainStep]
    providers: list[LLMProviderInfo]


class LLMChainUpdate(BaseModel):
    """Body for ``PUT /admin/llm-chain`` — full replacement."""

    steps: list[LLMChainStep]


__all__ = [
    "LLMChainStep",
    "LLMChainUpdate",
    "LLMChainRead",
    "LLMProviderInfo",
]