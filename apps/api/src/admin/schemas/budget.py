"""Pydantic schemas for tenant budget admin endpoints.

M4.D Task 4 — admin budget config + usage snapshot.

Field notes
-----------

* ``soft_warn_tokens`` / ``hard_cap_tokens`` — both optional. ``None``
  means "no warn / no cap" (the budget layer treats ``None`` as
  opt-out for that field).
* ``period_anchor_tz`` — IANA timezone name (e.g. ``"UTC"``,
  ``"America/Los_Angeles"``). Capped at 64 chars to match the
  column width in ``tenant_budgets.period_anchor_tz``.
* ``tokens_used`` — current period cumulative tokens; ``0`` for a
  fresh tenant with no ``llm_usage`` rows.
* ``period_starts_at`` — wall-clock start of the current period in
  ``period_anchor_tz`` (the budget layer's :func:`_current_period`
  computes this).
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TenantBudgetCreate(BaseModel):
    """Request body for POST /admin/tenants/{tenant_id}/budget."""

    soft_warn_tokens: int | None = Field(
        default=None, ge=0,
        description="Token count at which a one-shot soft warn fires.",
    )
    hard_cap_tokens: int | None = Field(
        default=None, ge=0,
        description="Token count at which LLM calls are blocked.",
    )
    period_anchor_tz: str = Field(
        default="UTC", max_length=64,
        description="IANA timezone name anchoring the monthly period.",
    )


class TenantBudgetRead(BaseModel):
    """Response body — the budget config (no secrets, no usage)."""

    soft_warn_tokens: int | None
    hard_cap_tokens: int | None
    period_anchor_tz: str
    updated_at: datetime


class TenantBudgetUsageRead(BaseModel):
    """Response body — live usage snapshot for the current period."""

    period: str
    tokens_used: int
    soft_warn_tokens: int | None
    hard_cap_tokens: int | None
    period_starts_at: datetime


class CleanupResponse(BaseModel):
    """Response body for POST /admin/budget/cleanup."""

    deleted_rows: int
    cutoff_period: str  # YYYY-MM


__all__ = [
    "CleanupResponse",
    "TenantBudgetCreate",
    "TenantBudgetRead",
    "TenantBudgetUsageRead",
]
