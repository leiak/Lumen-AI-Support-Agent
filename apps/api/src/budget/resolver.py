"""BudgetResolver — pre-check + post-record budget enforcement.

Wraps any :class:`Resolver` (typically :class:`TenantResolver` from M4.C)
to enforce a monthly per-tenant token hard cap. Implements the M4.A
``Resolver`` seam so :class:`LLMClient` consumes it without changes.

Public surface:
- :class:`BudgetResolver` — the wrapper.
- :class:`TenantBudgetExceeded` — raised at pre-check when at cap
  (defined in ``llm_client.exceptions`` for caller consistency).

Pre-check:
1. If ``budget is None`` or ``hard_cap_tokens is None`` → no enforcement.
2. Else load snapshot via cache; if ``tokens_used >= hard_cap_tokens``
   → increment ``LLM_TENANT_BUDGET_EXCEEDED_TOTAL`` and raise.

Post-record (after successful delegation):
1. Compute ``new_used = snapshot.tokens_used + resp.prompt_tokens + resp.completion_tokens``.
2. Call ``snapshot_repo.set_tokens_used(...)`` (UPSERT).
3. If ``snapshot.tokens_used < soft_warn_tokens <= new_used`` → fire
   one-shot soft warn: increment metric + structured log line.

The post-record path also invalidates the cache after the UPSERT so the
next pre-check sees the fresh ``tokens_used`` value (avoids
double-firing the soft-warn counter on subsequent calls within the
same period).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from budget.cache import TenantBudgetSnapshotCache
from budget.models import TenantBudget
from budget.repository import TenantBudgetSnapshotRepository
from core.business_metrics import (
    LLM_TENANT_BUDGET_EXCEEDED_TOTAL,
    LLM_TENANT_BUDGET_SOFT_WARN_TOTAL,
)
from llm_client.exceptions import TenantBudgetExceeded
from llm_client.resolvers import Resolver
from llm_client.types import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)


def _current_period(tz_name: str = "UTC") -> tuple[str, datetime]:
    """Return ``(period_string, period_start_datetime)`` for the given timezone.

    Period format: ``YYYY-MM``. Period start: 1st of the month at 00:00
    in the configured timezone (default UTC). Tests can override by
    patching ``budget.resolver._current_period``.
    """
    now = datetime.now(timezone.utc)
    period = now.strftime("%Y-%m")
    # 1st of month at 00:00 UTC
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return period, period_start


class BudgetResolver:
    """Wraps an inner resolver with monthly token hard-cap enforcement."""

    def __init__(
        self,
        *,
        inner: Resolver,
        tenant_id: str,
        budget: TenantBudget | None,
        snapshot_cache: TenantBudgetSnapshotCache,
        snapshot_repo: TenantBudgetSnapshotRepository | None = None,
    ) -> None:
        self._inner = inner
        self._tenant_id = tenant_id
        self._budget = budget
        self._snapshot_cache = snapshot_cache
        self._snapshot_repo = snapshot_repo or TenantBudgetSnapshotRepository()

    def __call__(self, request: ChatRequest) -> Any:
        """Sync ``Resolver`` shim (M4.A protocol compatibility).

        Pre-checks only — the M4.B chain execution path uses
        :meth:`ainvoke` where the full pre-check + delegate + post-record
        cycle runs.
        """
        self._pre_check_sync()
        return self._inner(request)

    async def ainvoke(self, request: ChatRequest) -> ChatResponse:
        """Async seam: pre-check, delegate, post-record.

        Returns:
            The :class:`ChatResponse` from the inner resolver.

        Raises:
            TenantBudgetExceeded: ``tokens_used >= hard_cap_tokens`` at
                pre-check (no delegate call made).
        """
        if self._budget is None:
            # Opt-out: no enforcement, no snapshot reads or writes.
            return await self._inner.ainvoke(request)
        period, period_start = await self._pre_check()
        resp = await self._inner.ainvoke(request)
        tokens_consumed = (
            getattr(resp, "prompt_tokens", 0) + getattr(resp, "completion_tokens", 0)
        )
        if tokens_consumed > 0:
            await self._post_record(period, period_start, tokens_consumed)
        return resp

    # ---- pre-check ---------------------------------------------------------

    def _pre_check_sync(self) -> None:
        """Sync pre-check helper used by ``__call__``.

        Computes the current period lazily (sync path doesn't use
        async cache loading — the resolver is constructed against a
        warm cache so this is cheap). If the budget row is missing or
        ``hard_cap_tokens`` is ``None``, returns silently.
        """
        if self._budget is None or self._budget.hard_cap_tokens is None:
            return
        period, period_start = _current_period(self._budget.period_anchor_tz)
        snap = self._snapshot_cache.get_or_load(
            self._tenant_id,
            period=period,
            period_starts_at=period_start,
        )
        if snap.tokens_used >= self._budget.hard_cap_tokens:
            LLM_TENANT_BUDGET_EXCEEDED_TOTAL.inc()
            raise TenantBudgetExceeded(
                tenant_id=self._tenant_id,
                period=period,
                tokens_used=snap.tokens_used,
                hard_cap_tokens=self._budget.hard_cap_tokens,
                period_starts_at=period_start,
            )

    async def _pre_check(self) -> tuple[str, datetime]:
        """Async pre-check: load snapshot via cache; raise if at cap.

        Returns:
            ``(period, period_start)`` tuple for reuse by ``_post_record``.

        Raises:
            TenantBudgetExceeded: when ``tokens_used >= hard_cap_tokens``.
        """
        if self._budget is None or self._budget.hard_cap_tokens is None:
            period, period_start = _current_period("UTC")
            return period, period_start
        period, period_start = _current_period(self._budget.period_anchor_tz)
        snap = await self._snapshot_cache.get_or_load_async(
            self._tenant_id,
            period=period,
            period_starts_at=period_start,
        )
        if snap.tokens_used >= self._budget.hard_cap_tokens:
            LLM_TENANT_BUDGET_EXCEEDED_TOTAL.inc()
            raise TenantBudgetExceeded(
                tenant_id=self._tenant_id,
                period=period,
                tokens_used=snap.tokens_used,
                hard_cap_tokens=self._budget.hard_cap_tokens,
                period_starts_at=period_start,
            )
        return period, period_start

    # ---- post-record -------------------------------------------------------

    async def _post_record(
        self,
        period: str,
        period_start: datetime,
        tokens_consumed: int,
    ) -> None:
        """Increment snapshot, invalidate cache, fire soft-warn on threshold cross.

        Soft-warn fires once: ``snapshot.tokens_used < soft_warn_tokens <= new_used``.
        The invalidate step is what guarantees this — the next pre-check reads
        the updated row (or a fresh refresh) instead of the cached value.
        """
        snap = await self._snapshot_cache.get_or_load_async(
            self._tenant_id,
            period=period,
            period_starts_at=period_start,
        )
        new_used = snap.tokens_used + tokens_consumed
        await self._snapshot_repo.set_tokens_used(
            tenant_id=self._tenant_id,
            period=period,
            tokens_used=new_used,
        )
        # CRITICAL: invalidate cache using period= kwarg (not just tenant_id)
        # so the next pre-check sees the fresh value.
        self._snapshot_cache.invalidate(self._tenant_id, period=period)
        # Soft-warn: fires once when this call CROSSES the threshold.
        if (
            self._budget is not None
            and self._budget.soft_warn_tokens is not None
            and snap.tokens_used < self._budget.soft_warn_tokens <= new_used
        ):
            LLM_TENANT_BUDGET_SOFT_WARN_TOTAL.inc()
            logger.warning(
                "tenant_budget.soft_warn",
                extra={
                    "tenant_id": self._tenant_id,
                    "period": period,
                    "tokens_used": new_used,
                    "soft_warn_tokens": self._budget.soft_warn_tokens,
                    "hard_cap_tokens": self._budget.hard_cap_tokens,
                },
            )


__all__ = ["BudgetResolver", "_current_period"]