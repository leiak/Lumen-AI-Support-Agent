"""ORM models for the M4.D budget layer.

Two tables:
- ``tenant_budgets``: per-tenant cap config (one row per tenant).
- ``tenant_budget_snapshots``: cached SUM(llm_usage) per (tenant, period).

Snapshot semantics: refresh on cache miss, increment in-place on
post-record. Period format is ``YYYY-MM`` (string for index locality).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class TenantBudget(Base):
    """One row per tenant — cap config.

    NULL fields = "unlimited / no warn". Tenant without a row here is
    not budget-enforced (M4.D is opt-in).
    """

    __tablename__ = "tenant_budgets"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    soft_warn_tokens: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    hard_cap_tokens: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    period_anchor_tz: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="UTC"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantBudgetSnapshot(Base):
    """One row per (tenant, period) — running total of tokens consumed.

    Period format: ``YYYY-MM`` (string). Refreshed against ``llm_usage``
    on cache miss; incremented in-place on post-record.
    """

    __tablename__ = "tenant_budget_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "period",
            name="uq_tenant_budget_snapshots_tenant_period",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    tokens_used: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    last_refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


__all__ = ["TenantBudget", "TenantBudgetSnapshot"]
