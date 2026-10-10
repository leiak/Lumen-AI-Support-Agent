"""Credit grant service for M4.D Pack B #2.

Append-only audit log of super_admin credit grants. Effective cap is
computed downstream in BudgetResolver as
``hard_cap_tokens + sum_for_period(tenant_id, period)``.

The service is the sole writer for :class:`TenantBudgetCredit` rows.
Rows are immutable in app logic — reapplying the business invariant
``tokens > 0`` and ``note.strip() != ""`` at the service boundary means
the DB check constraint + the application check agree.

The per-model cache invalidation call is present even though
:class:`PerModelBreakdownCache` ships in Task 4: Task 3's resolver +
Task 4's breakdown endpoint rely on the cache being invalidated when
a credit is granted (the effective cap changed).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from budget.models import TenantBudgetCredit
from budget.resolver import _current_period
from core.id_gen import new_id

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class CreditService:
    """Insert + query immutable ``tenant_budget_credits`` rows.

    Args:
        session: Async SQLAlchemy session — the service does NOT call
            ``commit()`` itself; the caller owns the transaction
            boundary (matches Pack A's repositories).
        per_model_cache: A ``PerModelBreakdownCache``-shaped object with
            an ``invalidate(tenant_id, period)`` method. The cache
            implementation ships in Task 4; the service accepts any
            duck-typed object that exposes ``invalidate`` so unit
            tests can use a MagicMock and Task 4 can wire the real
            singleton without changing this class.
    """

    def __init__(self, session: AsyncSession, per_model_cache: object) -> None:
        self._session = session
        self._per_model_cache = per_model_cache

    async def grant(
        self,
        *,
        tenant_id: str,
        tokens: int,
        note: str,
        granted_by: str,
    ) -> TenantBudgetCredit:
        """Insert an immutable audit row for a super_admin credit grant.

        Validates ``tokens > 0`` and ``note.strip() != ""``. Period bound
        to the current UTC month (denormalized on the row for index
        locality — matches Pack A's snapshot semantics).

        Side effect: invalidates the per-model cache for
        ``(tenant_id, period)`` so the next breakdown query reflects
        the newly granted credit (effective_cap changed).
        """
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        if not note.strip():
            raise ValueError("note must be non-empty")
        # Credits are always UTC-month-bound regardless of the tenant's
        # period_anchor_tz — credit grants are a global super_admin
        # operation, not a tenant-relative one.
        period, _ = _current_period("UTC")
        credit = TenantBudgetCredit(
            id=new_id(),
            tenant_id=tenant_id,
            period=period,
            tokens=tokens,
            note=note.strip(),
            granted_by=granted_by,
        )
        self._session.add(credit)
        await self._session.flush()
        # Invalidate per-model cache so the next breakdown query reflects
        # the newly granted credit (effective_cap changed).
        self._per_model_cache.invalidate(tenant_id, period=period)
        return credit

    async def sum_for_period(self, tenant_id: str, period: str) -> int:
        """SUM(tokens) for the given (tenant, period).

        Returns 0 if no rows match — never NULL — so callers can do
        ``base + sum_for_period`` without coalescing.
        """
        result = await self._session.execute(
            select(func.coalesce(func.sum(TenantBudgetCredit.tokens), 0))
            .where(TenantBudgetCredit.tenant_id == tenant_id)
            .where(TenantBudgetCredit.period == period)
        )
        return int(result.scalar_one())

    async def list_for_period(
        self, tenant_id: str, period: str
    ) -> list[TenantBudgetCredit]:
        """All credit rows for (tenant, period), ordered by created_at ASC.

        Earliest-first ordering matches the natural audit-log read
        direction (chronological — "what was granted when?").
        """
        result = await self._session.execute(
            select(TenantBudgetCredit)
            .where(TenantBudgetCredit.tenant_id == tenant_id)
            .where(TenantBudgetCredit.period == period)
            .order_by(TenantBudgetCredit.created_at.asc())
        )
        return list(result.scalars().all())


__all__ = ["CreditService"]