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
from zoneinfo import ZoneInfo

from budget.cache import TenantBudgetSnapshotCache
from budget.exceptions import TenantBudgetRateLimited
from budget.models import TenantBudget
from budget.precheck_cache import PrecheckCache
from budget.repository import TenantBudgetSnapshotRepository
from core.business_metrics import (
    LLM_BUDGET_GATE_TOTAL,
    LLM_TENANT_BUDGET_EXCEEDED_TOTAL,
    LLM_TENANT_BUDGET_SOFT_WARN_TOTAL,
)
from core.config import get_settings
from llm_client.exceptions import RateLimited, TenantBudgetExceeded
from llm_client.tenant_resolver import _NoChainConfigured
from llm_client.resolvers import Resolver
from llm_client.types import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)


def _current_period(tz_name: str = "UTC") -> tuple[str, datetime]:
    """Return (period, period_start) for the given IANA timezone.

    Period format: ``YYYY-MM``. Period start: 1st of the month at 00:00
    in the configured timezone (default UTC). Used by the budget
    pre-check + post-record path so the snapshot table aggregates
    align with the tenant's local calendar month, not UTC.

    Falls back to UTC if ``ZoneInfo(tz_name)`` raises (e.g. malformed
    ``period_anchor_tz`` config) so the resolver never crashes.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    now = datetime.now(tz)
    period = now.strftime("%Y-%m")
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
        credit_service: Any | None = None,
        per_model_cache: Any | None = None,
        precheck_cache: PrecheckCache | None = None,
    ) -> None:
        self._inner = inner
        self._tenant_id = tenant_id
        self._budget = budget
        self._snapshot_cache = snapshot_cache
        self._snapshot_repo = snapshot_repo or TenantBudgetSnapshotRepository()
        # Pack B #2: optional CreditService for effective_cap = base + credits.
        # When None, the resolver falls back to base-only enforcement
        # (backward compatible with the Pack A constructor signature).
        self._credit_service = credit_service
        # Pack B #5: reserved for Task 4 — accepts a PerModelBreakdownCache
        # so we don't need to modify this constructor again when Task 4
        # wires the invalidate calls into _post_record.
        self._per_model_cache = per_model_cache
        # Pack B follow-up — in-process 5s TTL cache for the pre-check
        # cap-decision result. Hot tenants with no new llm_usage rows
        # between turns pay zero DB round-trips. Optional for backward
        # compatibility with Pack A test fixtures that don't pass one.
        self._precheck_cache = precheck_cache

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
            TenantBudgetExceeded: ``tokens_used >= effective_cap`` at
                pre-check (no delegate call made). Note the payload's
                ``hard_cap_tokens`` field carries ``effective_cap``
                (base + credits) when credits are configured — the
                field name is preserved for backward compatibility with
                Pack A callers, but the value reflects the cap that was
                actually crossed.
        """
        if self._budget is None:
            # Opt-out: no enforcement, no snapshot reads or writes.
            return await self._inner.ainvoke(request)
        period, period_start = await self._pre_check()
        try:
            resp = await self._inner.ainvoke(request)
        except _NoChainConfigured:
            # Single-provider tenant — replicate LLMClient's retry loop so
            # _post_record still runs and cap tracking stays consistent.
            primary = self._inner(request)  # sync __call__ returns primary provider
            resp = await primary.chat(request)
        except RateLimited:
            # Provider chain exhausted on 429. Invalidate per-model cache
            # (a request did hit a provider), do NOT post_record (no tokens
            # billed), then check budget gate.
            if self._per_model_cache is not None:
                self._per_model_cache.invalidate(self._tenant_id, period)
            settings = get_settings()
            await self._maybe_raise_budget_gate(period, period_start, settings)
            raise  # gate did not fire → propagate 429
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
        """Async pre-check: load snapshot from DB (Pack A #3) and raise if at cap.

        Pack A #3: when ``tenant_budget_pre_check_use_db`` is True (default),
        bypasses the in-process LRU cache and reads ``tenant_budget_snapshots``
        directly. This closes the ≤60s overshoot window documented in M4.D §11
        (tech-debt #3). When False, falls back to the M4.D cache path
        (soft escape hatch for tenants that tolerate up-to-one-call overshoot).

        Pack B follow-up: when a ``precheck_cache`` is wired in, the
        ``(snap, effective_cap)`` result is held for
        ``tenant_budget_precheck_cache_ttl_s`` (default 5s) so a hot
        tenant with no new ``llm_usage`` rows between turns pays zero
        DB round-trips for the pre-check. ``_post_record`` invalidates
        the entry on every successful token consumption.

        Returns:
            ``(period, period_start)`` tuple for reuse by ``_post_record``.

        Raises:
            TenantBudgetExceeded: when ``tokens_used >= hard_cap_tokens``.
        """
        if self._budget is None or self._budget.hard_cap_tokens is None:
            period, period_start = _current_period("UTC")
            return period, period_start
        period, period_start = _current_period(self._budget.period_anchor_tz)

        async def _load_cap_decision() -> tuple[Any, int]:
            """Load the current snapshot + effective cap.

            The tuple is the cache value — both fields are needed to
            decide ``tokens_used >= effective_cap`` downstream.
            """
            settings = get_settings()
            if settings.tenant_budget_pre_check_use_db:
                # DB-direct path (Pack A #3): zero window between cap and rejection.
                loaded = await self._snapshot_repo.get_for_tenant_period(
                    self._tenant_id, period
                )
                if loaded is None:
                    # No snapshot row yet — refresh via SUM(llm_usage) (which
                    # itself holds the advisory lock per Pack A #7).
                    loaded = await self._snapshot_repo.refresh(
                        tenant_id=self._tenant_id,
                        period=period,
                        period_starts_at=period_start,
                    )
            else:
                # Legacy cache path — preserved as soft escape hatch.
                loaded = await self._snapshot_cache.get_or_load_async(
                    self._tenant_id,
                    period=period,
                    period_starts_at=period_start,
                )
            # Pack B #2: effective_cap = hard_cap_tokens + sum(credits for period).
            cap = await self._compute_effective_cap(period)
            return loaded, cap

        if self._precheck_cache is not None:
            snap, effective_cap = await self._precheck_cache.get_or_load(
                self._tenant_id, period, _load_cap_decision
            )
        else:
            snap, effective_cap = await _load_cap_decision()
        if snap.tokens_used >= effective_cap:
            LLM_TENANT_BUDGET_EXCEEDED_TOTAL.inc()
            raise TenantBudgetExceeded(
                tenant_id=self._tenant_id,
                period=period,
                tokens_used=snap.tokens_used,
                hard_cap_tokens=effective_cap,
                period_starts_at=period_start,
            )
        return period, period_start

    # ---- effective cap (Pack B #2) -----------------------------------------

    async def _compute_effective_cap(self, period: str) -> int:
        """Spec §3.6: effective cap = ``hard_cap_tokens`` + sum(credits).

        ``CreditService.sum_for_period`` returns 0 when no rows match
        (never NULL), so the math is safe — no coalescing needed.

        When no ``credit_service`` is configured (Pack A constructor
        callers) we fall back to base-only enforcement. This preserves
        backward compatibility: existing call sites that don't pass a
        credit service see the same behavior as before.

        Sync shim caveat: ``_pre_check_sync`` cannot await
        ``sum_for_period`` and therefore uses the base ``hard_cap_tokens``
        directly. This is a known limitation of the M4.A sync protocol —
        in production all credit-bearing tenants go through ``ainvoke``.
        """
        # _pre_check returns early when budget is None OR hard_cap_tokens
        # is None, so by the time we get here ``self._budget`` is non-None.
        # Defensive ``or 0`` covers ``hard_cap_tokens is None`` anyway.
        base = self._budget.hard_cap_tokens or 0
        if self._credit_service is None:
            return base
        return base + await self._credit_service.sum_for_period(
            self._tenant_id, period
        )

    # ---- post-mortem gate (Pack B #8) ---------------------------------------

    async def _maybe_raise_budget_gate(
        self, period: str, period_start: datetime, settings: Any
    ) -> None:
        """Post-mortem gate: fire when remaining budget < threshold after 429.

        The gate is *post-mortem*: it observes AFTER the inner provider
        chain has already raised :class:`RateLimited`. It does NOT preempt
        (the pre-check already enforces ``tokens_used >= effective_cap``).

        When ``settings.tenant_budget_429_skip_threshold_tokens`` is 0 the
        gate is disabled — short-circuit happens before any DB reads so a
        disabled gate costs nothing.

        Effective cap (``base + credits``) is used rather than base
        alone, mirroring the pre-check semantics: tenants with
        outstanding credits should not have their runway artificially
        shrunk by a 429 observation.

        ``period_start`` is passed in (not recomputed) to match the value
        :meth:`_pre_check` used — prevents a month-boundary drift where
        the gate observes ``(period=N+1)`` while the snapshot UPSERT
        still references ``period_start`` from period N.
        """
        threshold = settings.tenant_budget_429_skip_threshold_tokens
        if threshold == 0:
            return  # gate disabled — short-circuit before any DB reads
        snap = await self._snapshot_repo.get_for_tenant_period(
            self._tenant_id, period
        )
        if snap is None:
            # No snapshot row yet — refresh via SUM(llm_usage).
            snap = await self._snapshot_repo.refresh(
                tenant_id=self._tenant_id,
                period=period,
                period_starts_at=period_start,
            )
        effective_cap = await self._compute_effective_cap(period)
        remaining = effective_cap - snap.tokens_used
        if remaining < threshold:
            LLM_BUDGET_GATE_TOTAL.inc()
            raise TenantBudgetRateLimited(
                tenant_id=self._tenant_id,
                period=period,
                remaining=remaining,
                threshold=threshold,
            )

    # ---- post-record -------------------------------------------------------

    async def _post_record(
        self,
        period: str,
        period_start: datetime,
        tokens_consumed: int,
    ) -> None:
        """Increment snapshot, invalidate cache, fire sticky soft-warn.

        Pack A #4: soft-warn fires AT MOST ONCE per (tenant, period). The
        ``tenant_budget_snapshots.soft_warn_fired_at`` column records the
        fire timestamp; subsequent calls that would have crossed the
        threshold are no-ops (the ``snap.soft_warn_fired_at IS NULL``
        check is the gate).

        Pack A #3: ``set_tokens_used`` writes ``soft_warn_fired_at`` when
        firing (new value) or when an explicit non-None value is passed
        (carry-over from existing row). NULL on input preserves the
        existing timestamp.
        """
        snap = await self._snapshot_repo.get_for_tenant_period(
            self._tenant_id, period
        )
        if snap is None:
            snap = await self._snapshot_repo.refresh(
                tenant_id=self._tenant_id,
                period=period,
                period_starts_at=period_start,
            )
        new_used = snap.tokens_used + tokens_consumed

        # Pack A #4 sticky soft-warn check:
        #   - budget.soft_warn_tokens must be configured
        #   - snap.soft_warn_fired_at must be NULL (not yet fired this period)
        #   - the OLD usage was below threshold, the NEW usage is at/above
        fire_soft_warn = (
            self._budget is not None
            and self._budget.soft_warn_tokens is not None
            and snap.soft_warn_fired_at is None
            and snap.tokens_used < self._budget.soft_warn_tokens <= new_used
        )
        soft_warn_fired_at: datetime | None = snap.soft_warn_fired_at
        if fire_soft_warn:
            LLM_TENANT_BUDGET_SOFT_WARN_TOTAL.inc()
            soft_warn_fired_at = datetime.now(timezone.utc)
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
        await self._snapshot_repo.set_tokens_used(
            tenant_id=self._tenant_id,
            period=period,
            tokens_used=new_used,
            soft_warn_fired_at=soft_warn_fired_at,
        )
        # CRITICAL: invalidate cache using period= kwarg (not just tenant_id).
        self._snapshot_cache.invalidate(self._tenant_id, period=period)
        # Pack B #5: invalidate per-model cache so the next breakdown
        # query reflects this new llm_usage row. The cache is optional
        # (older Pack A callers don't pass one) — guard with None-check.
        if self._per_model_cache is not None:
            self._per_model_cache.invalidate(self._tenant_id, period)
        # Pack B follow-up — invalidate the pre-check cache so the next
        # _pre_check re-loads (snap, effective_cap) and observes the new
        # tokens_used total. Without this, a hot tenant could cache a
        # stale "below cap" decision for the full TTL after a write.
        if self._precheck_cache is not None:
            await self._precheck_cache.invalidate(self._tenant_id, period)


__all__ = ["BudgetResolver", "_current_period"]