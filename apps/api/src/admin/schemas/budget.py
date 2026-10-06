"""Pydantic schemas for tenant budget admin endpoints.

M4.D Task 4 — admin budget config + usage snapshot.
M4.D Pack B #2 — credit grant + list schemas (``CreditRequest``,
``CreditResponse``, ``CreditListResponse``).

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

from pydantic import BaseModel, ConfigDict, Field


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


class BreakdownItem(BaseModel):
    """One row of the per-model breakdown (Pack B #5).

    Mirrors :class:`budget.per_model.ModelUsage` field-for-field. The
    ``ModelUsage`` dataclass is service-layer (typed by SQLAlchemy);
    this Pydantic model is the wire shape consumed by the admin SPA.
    """

    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    request_count: int


class TenantBudgetUsageRead(BaseModel):
    """Response body — live usage snapshot for the current period.

    Pack B additions
    ----------------

    * ``effective_cap`` — ``hard_cap_tokens + sum(credits for current
      period)``. Same value the resolver pre-check uses, so the admin
      SPA can show the *actual* cap rather than the base config.
    * ``credits_total`` — sum of ``tenant_budget_credits.tokens`` for
      the current period. Always 0 when no grants exist (never NULL).
    * ``breakdown`` — per-provider/per-model token sums. Populated only
      when the request carries ``?breakdown=true``; ``None`` otherwise
      (Pack A callers see no behavioral change).
    """

    period: str
    tokens_used: int
    soft_warn_tokens: int | None
    hard_cap_tokens: int | None
    period_starts_at: datetime
    effective_cap: int                                # NEW (Pack B #2)
    credits_total: int                                # NEW (Pack B #2)
    breakdown: list[BreakdownItem] | None = None      # NEW (Pack B #5)


class CleanupResponse(BaseModel):
    """Response body for POST /admin/budget/cleanup."""

    deleted_rows: int
    cutoff_period: str  # YYYY-MM


# ---------------------------------------------------------------------------
# Pack B #2 — credit grant schemas.
#
# ``CreditRequest`` is the body for POST /admin/tenants/{tid}/credits.
# ``CreditResponse`` is the per-row response shape; ``CreditListResponse``
# wraps the array + sum for GET /admin/tenants/{tid}/credits.
# ``from_attributes = True`` on CreditResponse lets the route handler
# pass an ORM ``TenantBudgetCredit`` directly to ``model_validate`` —
# matches the read pattern used by TenantBudgetRead for TenantBudget rows.
# ---------------------------------------------------------------------------


class CreditRequest(BaseModel):
    """Request body for POST /admin/tenants/{tenant_id}/credits.

    ``tokens`` must be positive (validated both here and by the DB
    check constraint + the service-level guard). ``note`` is required
    and bounded at 500 chars to keep the audit log readable.
    """

    tokens: int = Field(..., gt=0)
    note: str = Field(..., min_length=1, max_length=500)


class CreditResponse(BaseModel):
    """Response body — single credit grant row.

    Mirrors the :class:`TenantBudgetCredit` ORM column-for-column;
    ``model_validate(credit_row)`` populates all fields including the
    server-stamped ``id``, ``period``, ``granted_by``, ``created_at``.
    """

    id: str
    tenant_id: str
    period: str
    tokens: int
    note: str
    granted_by: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CreditListResponse(BaseModel):
    """Response body for GET /admin/tenants/{tenant_id}/credits."""

    credits: list[CreditResponse]
    total_tokens: int


__all__ = [
    "BreakdownItem",
    "CleanupResponse",
    "CreditListResponse",
    "CreditRequest",
    "CreditResponse",
    "TenantBudgetCreate",
    "TenantBudgetRead",
    "TenantBudgetUsageRead",
]
