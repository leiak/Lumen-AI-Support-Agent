"""CRUD + refresh for ``tenant_budgets`` + ``tenant_budget_snapshots``.

The snapshot refresh path runs ``SUM(prompt_tokens + completion_tokens)``
over ``llm_usage`` for the given (tenant, period) — only on cache miss.
Post-record uses ``set_tokens_used`` which does an UPSERT on
``UNIQUE(tenant_id, period)`` so concurrent updates don't lose data.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from budget.models import TenantBudget, TenantBudgetSnapshot
from core.database import get_sessionmaker
from core.id_gen import new_id


class TenantBudgetRepository:
    """CRUD for ``TenantBudget`` rows (one per tenant)."""

    async def get_by_tenant(self, tenant_id: str) -> TenantBudget | None:
        """Return the single budget row for ``tenant_id`` or ``None``."""
        sm = get_sessionmaker()
        async with sm() as session:
            result = await session.execute(
                select(TenantBudget).where(TenantBudget.tenant_id == tenant_id)
            )
            return result.scalar_one_or_none()

    async def upsert(
        self,
        *,
        tenant_id: str,
        soft_warn_tokens: int | None = None,
        hard_cap_tokens: int | None = None,
        period_anchor_tz: str = "UTC",
    ) -> TenantBudget:
        """Insert or update the (single) budget row for a tenant.

        Uses INSERT ... ON CONFLICT to atomically replace the row.
        """
        sm = get_sessionmaker()
        async with sm() as session:
            stmt = (
                pg_insert(TenantBudget)
                .values(
                    id=new_id(),
                    tenant_id=tenant_id,
                    soft_warn_tokens=soft_warn_tokens,
                    hard_cap_tokens=hard_cap_tokens,
                    period_anchor_tz=period_anchor_tz,
                )
                .on_conflict_do_update(
                    index_elements=["tenant_id"],
                    set_={
                        "soft_warn_tokens": soft_warn_tokens,
                        "hard_cap_tokens": hard_cap_tokens,
                        "period_anchor_tz": period_anchor_tz,
                    },
                )
                .returning(TenantBudget)
            )
            result = await session.execute(stmt)
            row_obj = result.scalar_one()
            await session.commit()
            await session.refresh(row_obj)
            return row_obj


class TenantBudgetSnapshotRepository:
    """Read + upsert for ``TenantBudgetSnapshot`` rows."""

    async def get_for_tenant_period(
        self, tenant_id: str, period: str
    ) -> TenantBudgetSnapshot | None:
        """Return the snapshot row for (tenant, period) or ``None``."""
        sm = get_sessionmaker()
        async with sm() as session:
            result = await session.execute(
                select(TenantBudgetSnapshot).where(
                    TenantBudgetSnapshot.tenant_id == tenant_id,
                    TenantBudgetSnapshot.period == period,
                )
            )
            return result.scalar_one_or_none()

    async def set_tokens_used(
        self,
        *,
        tenant_id: str,
        period: str,
        tokens_used: int,
        soft_warn_fired_at: datetime | None = None,
    ) -> TenantBudgetSnapshot:
        """Upsert tokens_used (and optionally soft_warn_fired_at) for (tenant_id, period).

        Pack A #4: when ``soft_warn_fired_at`` is None on input, the existing
        row's value is preserved (no NULL overwrite). This is the carry-over
        behavior that keeps sticky soft-warn from accidentally clearing the
        original fire timestamp on subsequent calls in the same period.

        Concurrent calls: the second one wins (last-writer-wins). Acceptable
        for budget tracking — exact values aren't billing-grade.
        """
        sm = get_sessionmaker()
        async with sm() as session:
            set_clause: dict[str, Any] = {"tokens_used": tokens_used}
            # Pack A #4: only overwrite soft_warn_fired_at when an explicit
            # non-None value is provided. Passing None leaves the existing
            # value untouched (sticky behavior).
            if soft_warn_fired_at is not None:
                set_clause["soft_warn_fired_at"] = soft_warn_fired_at
            stmt = (
                pg_insert(TenantBudgetSnapshot)
                .values(
                    id=new_id(),
                    tenant_id=tenant_id,
                    period=period,
                    tokens_used=tokens_used,
                    soft_warn_fired_at=soft_warn_fired_at,
                )
                .on_conflict_do_update(
                    constraint="uq_tenant_budget_snapshots_tenant_period",
                    set_=set_clause,
                )
                .returning(TenantBudgetSnapshot)
            )
            result = await session.execute(stmt)
            row_obj = result.scalar_one()
            await session.commit()
            await session.refresh(row_obj)
            return row_obj

    async def refresh(
        self,
        *,
        tenant_id: str,
        period: str,
        period_starts_at: datetime,
    ) -> TenantBudgetSnapshot:
        """Run ``SUM(prompt+completion)`` on llm_usage and write a snapshot.

        Called on cache miss when no row exists for the current period.
        Idempotent: re-running refreshes the cached value.
        """
        # Late import to avoid circular dep with llm_client.models
        from llm_client.models import LLMUsage

        # LLMUsage.created_at is TIMESTAMP WITHOUT TIME ZONE — strip tz if present
        # so asyncpg can bind the value without offset-naive/aware mismatch.
        if period_starts_at.tzinfo is not None:
            period_starts_at = period_starts_at.replace(tzinfo=None)

        sm = get_sessionmaker()
        async with sm() as session:
            sum_expr = func.coalesce(
                func.sum(LLMUsage.prompt_tokens + LLMUsage.completion_tokens), 0
            )
            result = await session.execute(
                select(sum_expr).where(
                    LLMUsage.tenant_id == tenant_id,
                    LLMUsage.created_at >= period_starts_at,
                )
            )
            total = int(result.scalar_one())
            stmt = (
                pg_insert(TenantBudgetSnapshot)
                .values(
                    id=new_id(),
                    tenant_id=tenant_id,
                    period=period,
                    tokens_used=total,
                )
                .on_conflict_do_update(
                    constraint="uq_tenant_budget_snapshots_tenant_period",
                    set_={
                        "tokens_used": total,
                        "last_refreshed_at": func.now(),
                    },
                )
                .returning(TenantBudgetSnapshot)
            )
            ins_result = await session.execute(stmt)
            row_obj = ins_result.scalar_one()
            await session.commit()
            await session.refresh(row_obj)
            return row_obj


__all__ = ["TenantBudgetRepository", "TenantBudgetSnapshotRepository"]
