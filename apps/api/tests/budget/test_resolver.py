"""Tests for BudgetResolver pre-check + post-record behavior."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from budget.models import TenantBudget, TenantBudgetSnapshot
from budget.resolver import BudgetResolver
from llm_client.exceptions import TenantBudgetExceeded
from llm_client.types import ChatRequest, ChatResponse


def _make_budget(
    *,
    soft_warn_tokens: int | None = 100,
    hard_cap_tokens: int | None = 200,
) -> Any:
    """Build a TenantBudget-like stub (no DB)."""
    b = MagicMock(spec=TenantBudget)
    b.soft_warn_tokens = soft_warn_tokens
    b.hard_cap_tokens = hard_cap_tokens
    b.period_anchor_tz = "UTC"
    return b


def _make_snapshot(tenant_id: str, period: str, tokens_used: int) -> Any:
    """Build a TenantBudgetSnapshot-like stub (no DB)."""
    s = MagicMock(spec=TenantBudgetSnapshot)
    s.tenant_id = tenant_id
    s.period = period
    s.tokens_used = tokens_used
    s.id = "snap-id"
    s.last_refreshed_at = datetime(2026, 10, 1, tzinfo=UTC)
    s.soft_warn_fired_at = None  # Pack A #4: default to "not yet fired"
    return s


def _make_response(prompt_tokens: int = 100, completion_tokens: int = 50) -> Any:
    """Build a ChatResponse-like stub."""
    r = MagicMock(spec=ChatResponse)
    r.prompt_tokens = prompt_tokens
    r.completion_tokens = completion_tokens
    return r


async def test_pre_check_pass_when_under_cap() -> None:
    """Snapshot below hard cap → inner resolver is invoked normally."""
    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response())
    cache = MagicMock()
    repo = MagicMock()
    snap = _make_snapshot("t1", "2026-10", 50)
    repo.get_for_tenant_period = AsyncMock(return_value=snap)
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(hard_cap_tokens=200),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    result = await resolver.ainvoke(MagicMock(spec=ChatRequest))
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 50
    # Inner was called exactly once
    inner.ainvoke.assert_awaited_once()
    # Post-record wrote the new total. We don't lock the
    # ``soft_warn_fired_at`` kwarg (Pack A #4 internal detail) — just
    # verify ``set_tokens_used`` was called with the right tokens_used.
    repo.set_tokens_used.assert_awaited()
    last_kwargs = repo.set_tokens_used.await_args.kwargs
    assert last_kwargs["tokens_used"] == 200
    assert last_kwargs["period"] == "2026-10"


async def test_pre_check_raises_when_at_cap() -> None:
    """Snapshot at/above hard cap → TenantBudgetExceeded raised, inner NOT called."""
    inner = MagicMock()
    inner.ainvoke = AsyncMock()
    cache = MagicMock()
    repo = MagicMock()
    # Pack A #3: pre-check now reads DB-direct via snapshot_repo.
    repo.get_for_tenant_period = AsyncMock(
        return_value=_make_snapshot("t1", "2026-10", 200)
    )
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(hard_cap_tokens=200),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    with pytest.raises(TenantBudgetExceeded) as exc:
        await resolver.ainvoke(MagicMock(spec=ChatRequest))
    assert exc.value.tenant_id == "t1"
    assert exc.value.tokens_used == 200
    assert exc.value.hard_cap_tokens == 200
    assert exc.value.period == "2026-10"
    # Inner resolver must NOT have been called
    inner.ainvoke.assert_not_called()


async def test_pre_check_no_budget_is_noop() -> None:
    """budget=None → BudgetResolver is a pass-through (no enforcement, no cache read)."""
    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response())
    cache = MagicMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=None,  # no budget configured
        snapshot_cache=cache,
    )
    result = await resolver.ainvoke(MagicMock(spec=ChatRequest))
    assert result.prompt_tokens == 100
    cache.get_or_load_async.assert_not_called()


async def test_post_record_increments_snapshot() -> None:
    """Post-record UPSERTs the snapshot with new total + invalidates cache."""
    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response(prompt_tokens=100, completion_tokens=50))
    cache = MagicMock()
    snap = _make_snapshot("t1", "2026-10", 0)
    repo = MagicMock()
    repo.get_for_tenant_period = AsyncMock(return_value=snap)
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    repo.set_tokens_used.assert_awaited()
    last_kwargs = repo.set_tokens_used.await_args.kwargs
    assert last_kwargs["tokens_used"] == 150
    # Cache invalidated with period kwarg (NOT just tenant_id)
    cache.invalidate.assert_called_once_with("t1", period="2026-10")


async def test_post_record_fires_soft_warn_once() -> None:
    """Soft-warn fires exactly once when THIS call crosses soft_warn_tokens.

    Each ``ainvoke`` reads the snapshot twice (pre-check + post-record,
    both via snapshot_repo.get_for_tenant_period in Pack A), so the
    iterator yields 4 entries — 99, 99 for the first call, then 149,
    149 for the second. The first call's pre-check + post-record both
    run against snap=99 → fires soft-warn (99 < 100 <= 149). The second
    call's pre-check + post-record both run against snap=149 → does NOT
    fire (149 < 100 is False). Net: +1 to the metric counter.
    """
    from core.business_metrics import LLM_TENANT_BUDGET_SOFT_WARN_TOTAL

    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response(prompt_tokens=100, completion_tokens=50))
    cache = MagicMock()

    snap_states = iter(
        [
            _make_snapshot("t1", "2026-10", 99),  # 1st ainvoke: pre-check
            _make_snapshot("t1", "2026-10", 99),  # 1st ainvoke: post-record
            _make_snapshot("t1", "2026-10", 149),  # 2nd ainvoke: pre-check (post-UPSERT)
            _make_snapshot("t1", "2026-10", 149),  # 2nd ainvoke: post-record
        ]
    )
    repo = MagicMock()
    repo.get_for_tenant_period = AsyncMock(side_effect=lambda *a, **kw: next(snap_states))
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(soft_warn_tokens=100, hard_cap_tokens=1000),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    before = LLM_TENANT_BUDGET_SOFT_WARN_TOTAL._value.get()  # type: ignore[attr-defined]
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    after = LLM_TENANT_BUDGET_SOFT_WARN_TOTAL._value.get()  # type: ignore[attr-defined]
    assert after - before == 1


async def test_exception_carries_full_context() -> None:
    """TenantBudgetExceeded carries tenant_id, period, tokens_used, hard_cap, period_starts_at."""
    inner = MagicMock()
    inner.ainvoke = AsyncMock()
    cache = MagicMock()
    repo = MagicMock()
    # Pack A #3: pre-check now reads DB-direct via snapshot_repo.
    repo.get_for_tenant_period = AsyncMock(
        return_value=_make_snapshot("t-xyz", "2026-10", 1000)
    )
    resolver = BudgetResolver(
        inner=inner, tenant_id="t-xyz",
        budget=_make_budget(hard_cap_tokens=500),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    with pytest.raises(TenantBudgetExceeded) as exc:
        await resolver.ainvoke(MagicMock(spec=ChatRequest))
    assert exc.value.tenant_id == "t-xyz"
    assert exc.value.period == "2026-10"
    assert exc.value.tokens_used == 1000
    assert exc.value.hard_cap_tokens == 500
    assert exc.value.period_starts_at is not None
    assert isinstance(exc.value.period_starts_at, datetime)


def test_current_period_honors_tz_name() -> None:
    """_current_period uses ZoneInfo(tz_name) for the period_start."""
    from budget.resolver import _current_period
    # Asia/Shanghai is UTC+8; March 1 00:00 SHA = Feb 28 16:00 UTC
    period, period_start = _current_period("Asia/Shanghai")
    assert len(period) == 7  # YYYY-MM
    # period_start is at 00:00 in Asia/Shanghai
    assert period_start.hour == 0
    assert period_start.minute == 0
    # Verify tz is correctly set (Asia/Shanghai is +08:00)
    assert period_start.utcoffset() == timedelta(hours=8)
    # Same test for UTC
    _period_utf8, period_start_utc = _current_period("UTC")
    assert period_start_utc.utcoffset() == timedelta(0)


def test_current_period_falls_back_to_utc_on_bad_malformed() -> None:
    """Malformed tz_name does not crash — falls back to UTC."""
    from budget.resolver import _current_period
    # "Not/A/Zone" is not a valid IANA name
    period, period_start = _current_period("Not/A/Zone")
    assert len(period) == 7
    assert period_start.utcoffset() == timedelta(0)  # UTC fallback


async def test_ainvoke_post_record_runs_for_single_provider_chain() -> None:
    """Single-provider tenant — BudgetResolver.ainvoke must run post_record even
    when the inner TenantResolverSpan.ainvoke raises _NoChainConfigured.

    Regression: without this fix, cap tracking is silently bypassed for
    tenants with tenant_budgets row + single provider + no fallback chain.
    """
    from llm_client.tenant_resolver import _NoChainConfigured

    primary_provider = MagicMock()
    response = MagicMock()
    response.prompt_tokens = 100
    response.completion_tokens = 50
    primary_provider.chat = AsyncMock(return_value=response)

    class _FakeInner:
        async def ainvoke(self, request):
            raise _NoChainConfigured()

        def __call__(self, request):
            return primary_provider

    inner = _FakeInner()

    cache = MagicMock()
    snap = _make_snapshot("t1", "2026-10", 0)
    repo = MagicMock()
    repo.get_for_tenant_period = AsyncMock(return_value=snap)
    repo.set_tokens_used = AsyncMock()

    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )

    resp = await resolver.ainvoke(MagicMock())
    assert resp is response
    # post_record must have run via the single-provider fallback path
    repo.set_tokens_used.assert_awaited()
    last_kwargs = repo.set_tokens_used.await_args.kwargs
    assert last_kwargs["tokens_used"] == 150


async def test_pre_check_db_direct_eliminates_ttl_window() -> None:
    """Pack A #3: _pre_check reads from snapshot_repo directly (not cache).

    Pre-populate DB-direct with tokens_used=7900 and hard_cap=7500.
    The cache returns a stale "100" value — if pre-check USED this it
    would let the call through. Test fails if implementation reads
    from cache instead of DB.
    """
    inner = MagicMock()
    inner.ainvoke = AsyncMock()
    cache = MagicMock()
    # Cache returns stale (low) value — proves DB-direct bypasses it.
    cache.get_or_load_async = AsyncMock(
        return_value=_make_snapshot("t1", "2026-10", 100)
    )
    cache.invalidate = MagicMock()
    repo = MagicMock()
    # DB-direct path returns the live value (above hard cap)
    repo.get_for_tenant_period = AsyncMock(
        return_value=_make_snapshot("t1", "2026-10", 7900)
    )
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(hard_cap_tokens=7500),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    # Pre-populate DB-direct: tokens_used=7900, hard_cap=7500 → reject.
    # Cache returns a stale "100" value — if pre-check USED this, it would
    # let the call through. Test fails if implementation reads from cache.
    with pytest.raises(TenantBudgetExceeded):
        await resolver.ainvoke(MagicMock(spec=ChatRequest))
    # Pre-check went through DB-direct, NOT the cache
    repo.get_for_tenant_period.assert_awaited()
    cache.get_or_load_async.assert_not_called()
    # Inner resolver was never invoked (rejection happened first)
    inner.ainvoke.assert_not_called()


async def test_soft_warn_fires_once_per_period_sticky() -> None:
    """Pack A #4: soft-warn fires exactly once per (tenant, period).

    First call crosses threshold → fires (+1 to metric) + sets soft_warn_fired_at.
    Second call also crosses (would have fired under old logic) → does NOT fire
    because soft_warn_fired_at IS NOT NULL (sticky).
    """
    from datetime import datetime as _dt

    from core.business_metrics import LLM_TENANT_BUDGET_SOFT_WARN_TOTAL

    inner = MagicMock()
    inner.ainvoke = AsyncMock(side_effect=[
        _make_response(prompt_tokens=100, completion_tokens=50),
        _make_response(prompt_tokens=100, completion_tokens=50),
    ])
    cache = MagicMock()

    # Build snapshots for the resolver's reads. Each call does:
    #   - DB-direct pre-check: 1 read via snapshot_repo.get_for_tenant_period
    #   - post-record: 1 read via snapshot_repo.get_for_tenant_period
    snap_with_no_fire = _make_snapshot("t1", "2026-10", 99)
    snap_with_no_fire.soft_warn_fired_at = None
    snap_after_fire = _make_snapshot("t1", "2026-10", 149)
    snap_after_fire.soft_warn_fired_at = _dt(2026, 10, 15, 12, 0, tzinfo=UTC)

    repo = MagicMock()
    repo.get_for_tenant_period = AsyncMock(side_effect=[
        snap_with_no_fire,  # call 1 pre-check: 99, no fire
        snap_with_no_fire,  # call 1 post-record: 99, no fire
        snap_after_fire,    # call 2 pre-check: 149, ALREADY FIRED
        snap_after_fire,    # call 2 post-record: 149, ALREADY FIRED
    ])
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(soft_warn_tokens=100, hard_cap_tokens=1000),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    before = LLM_TENANT_BUDGET_SOFT_WARN_TOTAL._value.get()  # type: ignore[attr-defined]
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    after = LLM_TENANT_BUDGET_SOFT_WARN_TOTAL._value.get()  # type: ignore[attr-defined]
    # Call 1: snap.tokens_used (99) < soft_warn (100) <= new_used (149) → fires
    # Call 2: snap.soft_warn_fired_at IS NOT NULL → no fire (sticky)
    assert after - before == 1
    # Both set_tokens_used calls preserved the fire timestamp
    assert repo.set_tokens_used.await_count == 2


async def test_pre_check_db_direct_when_setting_enabled_default() -> None:
    """Default settings: tenant_budget_pre_check_use_db=True → DB-direct path."""
    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response())
    cache = MagicMock()
    snap = _make_snapshot("t1", "2026-10", 100)
    repo = MagicMock()
    repo.get_for_tenant_period = AsyncMock(return_value=snap)
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(hard_cap_tokens=1000),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    # DB-direct path: repo.get_for_tenant_period called for pre-check
    assert repo.get_for_tenant_period.await_count >= 1
    # Cache was NOT consulted for the pre-check (only invalidated)
    cache.get_or_load_async.assert_not_called()


async def test_pre_check_cache_fallback_when_setting_disabled() -> None:
    """tenant_budget_pre_check_use_db=False → legacy cache path is used."""
    import os

    from core.config import reset_settings

    os.environ["TENANT_BUDGET_PRE_CHECK_USE_DB"] = "false"
    reset_settings()
    try:
        inner = MagicMock()
        inner.ainvoke = AsyncMock(return_value=_make_response())
        cache = MagicMock()
        cache.get_or_load_async = AsyncMock(
            return_value=_make_snapshot("t1", "2026-10", 100)
        )
        repo = MagicMock()
        # post_record still uses snapshot_repo (Pack A #4) regardless of
        # pre_check path, so we need to mock set_tokens_used.
        repo.get_for_tenant_period = AsyncMock(
            return_value=_make_snapshot("t1", "2026-10", 100)
        )
        repo.set_tokens_used = AsyncMock()
        resolver = BudgetResolver(
            inner=inner, tenant_id="t1",
            budget=_make_budget(hard_cap_tokens=1000),
            snapshot_cache=cache,
            snapshot_repo=repo,
        )
        await resolver.ainvoke(MagicMock(spec=ChatRequest))
        cache.get_or_load_async.assert_awaited()
        # Note: post_record (Pack A #4) always reads via snapshot_repo
        # regardless of pre_check path, so we don't assert_not_called on
        # ``repo.get_for_tenant_period`` here. The DB-direct pre-check
        # is excluded by the ``if settings.tenant_budget_pre_check_use_db``
        # branch in ``_pre_check`` — verified by ``cache.get_or_load_async``
        # being consulted.
    finally:
        os.environ.pop("TENANT_BUDGET_PRE_CHECK_USE_DB", None)
        reset_settings()


async def test_soft_warn_does_not_fire_when_already_fired() -> None:
    """Pre-populated soft_warn_fired_at → cross threshold does NOT fire."""
    from datetime import datetime as _dt

    from core.business_metrics import LLM_TENANT_BUDGET_SOFT_WARN_TOTAL

    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response(prompt_tokens=100, completion_tokens=50))
    cache = MagicMock()

    snap = _make_snapshot("t1", "2026-10", 99)
    snap.soft_warn_fired_at = _dt(2026, 10, 10, 0, 0, tzinfo=UTC)

    repo = MagicMock()
    repo.get_for_tenant_period = AsyncMock(side_effect=[
        snap,  # pre-check
        snap,  # post-record
    ])
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(soft_warn_tokens=100, hard_cap_tokens=1000),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    before = LLM_TENANT_BUDGET_SOFT_WARN_TOTAL._value.get()  # type: ignore[attr-defined]
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    after = LLM_TENANT_BUDGET_SOFT_WARN_TOTAL._value.get()  # type: ignore[attr-defined]
    # snap.soft_warn_fired_at IS NOT NULL → no fire
    assert after - before == 0
    # Carry-over: set_tokens_used receives the existing one, not None
    repo.set_tokens_used.assert_awaited_once()
    call_kwargs = repo.set_tokens_used.await_args.kwargs
    assert call_kwargs["soft_warn_fired_at"] == _dt(2026, 10, 10, 0, 0, tzinfo=UTC)


async def test_post_record_carries_over_soft_warn_fired_at() -> None:
    """Post-record preserves existing soft_warn_fired_at when not firing."""
    from datetime import datetime as _dt

    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response(prompt_tokens=100, completion_tokens=50))
    cache = MagicMock()
    existing_ts = _dt(2026, 10, 1, 12, 0, tzinfo=UTC)
    snap = _make_snapshot("t1", "2026-10", 100)  # already past soft_warn but no fire
    snap.soft_warn_fired_at = existing_ts  # but we previously fired

    repo = MagicMock()
    repo.get_for_tenant_period = AsyncMock(side_effect=[snap, snap])
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(soft_warn_tokens=50, hard_cap_tokens=1000),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    repo.set_tokens_used.assert_awaited_once()
    call_kwargs = repo.set_tokens_used.await_args.kwargs
    # Existing timestamp preserved (not overwritten with None)
    assert call_kwargs["soft_warn_fired_at"] == existing_ts


# ---------------------------------------------------------------------------
# Pack B #2 — BudgetResolver computes effective_cap = hard_cap_tokens + credits
# ---------------------------------------------------------------------------


async def test_pre_check_uses_effective_cap_includes_credits() -> None:
    """Pack B #2: effective_cap = hard_cap_tokens + sum_for_period(credits).

    Setup: hard_cap=8000, credits=2000 → effective_cap=10000.
    tokens_used=8500 should pass pre-check (8500 < 10000),
    even though 8500 > 8000 base.
    """
    budget = _make_budget(hard_cap_tokens=8000)
    snap = _make_snapshot("t1", "2026-10", tokens_used=8500)
    cache = MagicMock()
    # Legacy cache path isn't used when DB-direct is enabled (default),
    # but we mock it defensively for the fallback path.
    cache.get_or_load_async = AsyncMock(return_value=snap)
    snap_repo = MagicMock()
    snap_repo.get_for_tenant_period = AsyncMock(return_value=snap)
    snap_repo.set_tokens_used = AsyncMock()

    credit_svc = MagicMock()
    credit_svc.sum_for_period = AsyncMock(return_value=2000)

    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response(prompt_tokens=100, completion_tokens=50))

    resolver = BudgetResolver(
        inner=inner,
        tenant_id="t1",
        budget=budget,
        snapshot_cache=cache,
        snapshot_repo=snap_repo,
        credit_service=credit_svc,
    )
    result = await resolver.ainvoke(MagicMock(spec=ChatRequest))
    # Pre-check passed (no TenantBudgetExceeded raised).
    assert result.prompt_tokens == 100
    # Post-record wrote the new total.
    snap_repo.set_tokens_used.assert_awaited()
    # Credit sum was queried exactly once for the pre-check.
    credit_svc.sum_for_period.assert_awaited_once_with("t1", "2026-10")


async def test_pre_check_rejects_when_used_at_effective_cap() -> None:
    """Pack B #2: tokens_used=10000 >= effective_cap=10000 → TenantBudgetExceeded."""
    budget = _make_budget(hard_cap_tokens=8000)
    snap = _make_snapshot("t1", "2026-10", tokens_used=10000)
    cache = MagicMock()
    cache.get_or_load_async = AsyncMock(return_value=snap)
    snap_repo = MagicMock()
    snap_repo.get_for_tenant_period = AsyncMock(return_value=snap)
    snap_repo.set_tokens_used = AsyncMock()

    credit_svc = MagicMock()
    credit_svc.sum_for_period = AsyncMock(return_value=2000)

    inner = MagicMock()
    inner.ainvoke = AsyncMock()

    resolver = BudgetResolver(
        inner=inner,
        tenant_id="t1",
        budget=budget,
        snapshot_cache=cache,
        snapshot_repo=snap_repo,
        credit_service=credit_svc,
    )
    with pytest.raises(TenantBudgetExceeded):
        await resolver.ainvoke(MagicMock(spec=ChatRequest))
    # Inner resolver must NOT have been called (rejection happened first).
    inner.ainvoke.assert_not_called()
    # And no post-record write happened either.
    snap_repo.set_tokens_used.assert_not_called()
    # Credit sum was still queried (pre-check needs it).
    credit_svc.sum_for_period.assert_awaited_once_with("t1", "2026-10")


async def test_effective_cap_falls_back_when_no_credit_service() -> None:
    """Pack B #2: backward compat — credit_service=None → effective_cap = base.

    With hard_cap=8000 and tokens_used=8500 (no credits), pre-check should
    reject because 8500 >= 8000 base cap.
    """
    budget = _make_budget(hard_cap_tokens=8000)
    snap = _make_snapshot("t1", "2026-10", tokens_used=8500)
    cache = MagicMock()
    cache.get_or_load_async = AsyncMock(return_value=snap)
    snap_repo = MagicMock()
    snap_repo.get_for_tenant_period = AsyncMock(return_value=snap)

    inner = MagicMock()
    inner.ainvoke = AsyncMock()

    resolver = BudgetResolver(
        inner=inner,
        tenant_id="t1",
        budget=budget,
        snapshot_cache=cache,
        snapshot_repo=snap_repo,
        # credit_service omitted → backward compat: effective_cap = base
    )
    with pytest.raises(TenantBudgetExceeded) as exc:
        await resolver.ainvoke(MagicMock(spec=ChatRequest))
    # Exception still carries the BASE cap (8000), not effective_cap (8000 same here).
    assert exc.value.hard_cap_tokens == 8000
    # Inner resolver must NOT have been called.
    inner.ainvoke.assert_not_called()


async def test_post_record_invalidates_per_model_cache() -> None:
    """Pack B #5: _post_record must invalidate the per-model cache so the
    next ``GET /admin/tenants/{tid}/budget/usage?breakdown=true`` reflects
    this call's token consumption, not a 30s-stale snapshot.
    """
    from budget.per_model import PerModelBreakdownCache

    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response(prompt_tokens=100, completion_tokens=50))
    snap_cache = MagicMock()
    snap = _make_snapshot("t1", "2026-10", 0)
    snap_repo = MagicMock()
    snap_repo.get_for_tenant_period = AsyncMock(return_value=snap)
    snap_repo.set_tokens_used = AsyncMock()
    # Real PerModelBreakdownCache so we can assert invalidate() lands.
    per_model_cache = PerModelBreakdownCache(ttl_seconds=30)
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(),
        snapshot_cache=snap_cache,
        snapshot_repo=snap_repo,
        per_model_cache=per_model_cache,
    )
    # Pre-populate cache with a sentinel so we can assert it was cleared.
    from budget.per_model import ModelUsage
    per_model_cache.set("t1", "2026-10", [
        ModelUsage("openai", "gpt-4o-mini", 0, 0, 0, 0),
    ])
    assert per_model_cache.get("t1", "2026-10") is not None   # sanity
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    # Cache entry must be dropped (post-record invalidate).
    assert per_model_cache.get("t1", "2026-10") is None


async def test_post_record_skips_invalidate_when_no_cache() -> None:
    """Pack B #5 backward compat: resolver constructed without
    ``per_model_cache`` (Pack A callers) must not raise during _post_record.
    """
    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response())
    snap_cache = MagicMock()
    snap = _make_snapshot("t1", "2026-10", 0)
    snap_repo = MagicMock()
    snap_repo.get_for_tenant_period = AsyncMock(return_value=snap)
    snap_repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(),
        snapshot_cache=snap_cache,
        snapshot_repo=snap_repo,
        # per_model_cache omitted → no invalidate call, no AttributeError
    )
    # Must complete without raising.
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    snap_repo.set_tokens_used.assert_awaited()


async def test_ainvoke_handles_prefix_resolver_inner_without_ainvoke() -> None:
    """Single-provider tenant via real ``_PrefixResolver`` shape (no ``ainvoke``).

    Regression for the M4.D ↔ MiniMax-proxy demo bug: when a tenant has a
    ``tenant_budgets`` row + a single provider, ``_default_llm_client_factory``
    wraps the gateway's ``default_resolver`` (a :class:`_PrefixResolver`,
    which only implements the ``__call__`` ``Resolver`` protocol) inside a
    :class:`BudgetResolver`. The old code called ``self._inner.ainvoke(...)``
    directly and crashed with ``AttributeError`` instead of routing through
    the primary provider. The fix dispatches via ``__call__`` + ``provider.chat``
    so single-provider tenants with a budget row still get their calls tracked.
    """
    primary_provider = MagicMock()
    response = MagicMock()
    response.prompt_tokens = 100
    response.completion_tokens = 50
    primary_provider.chat = AsyncMock(return_value=response)

    class _PrefixResolverLike:
        """Mirror of llm_client.resolvers._PrefixResolver: __call__ only."""

        def __call__(self, request):
            return primary_provider

    inner = _PrefixResolverLike()  # NO ainvoke attribute at all

    cache = MagicMock()
    snap = _make_snapshot("t1", "2026-10", 0)
    repo = MagicMock()
    repo.get_for_tenant_period = AsyncMock(return_value=snap)
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )

    # The bug: this raised AttributeError ("'_PrefixResolver' object has no
    # attribute 'ainvoke'") and wrapped as ProviderUnavailable in
    # LLMClient.chat, breaking every agent-graph call for budget tenants.
    resp = await resolver.ainvoke(MagicMock(spec=ChatRequest))
    assert resp is response
    primary_provider.chat.assert_awaited_once()
    # post_record must still run so cap enforcement + soft-warn fire.
    repo.set_tokens_used.assert_awaited()
    last_kwargs = repo.set_tokens_used.await_args.kwargs
    assert last_kwargs["tokens_used"] == 150


async def test_ainvoke_handles_prefix_resolver_inner_when_no_budget() -> None:
    """budget=None + _PrefixResolver inner → still routes through __call__.

    The opt-out branch (``self._budget is None``) historically called
    ``self._inner.ainvoke(...)`` directly. Same AttributeError for
    _PrefixResolver inners. After the fix the helper centralizes dispatch.
    """
    primary_provider = MagicMock()
    response = MagicMock()
    response.prompt_tokens = 7
    response.completion_tokens = 3
    primary_provider.chat = AsyncMock(return_value=response)

    class _PrefixResolverLike:
        def __call__(self, request):
            return primary_provider

    inner = _PrefixResolverLike()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=None,
        snapshot_cache=MagicMock(),
    )
    resp = await resolver.ainvoke(MagicMock(spec=ChatRequest))
    assert resp is response
    primary_provider.chat.assert_awaited_once()


async def test_sync_call_in_async_context_skips_pre_check() -> None:
    """``__call__`` from inside a running event loop must NOT raise.

    Regression for the LLMClient.stream_chat bug: ``stream_chat`` resolves
    the provider via the sync ``__call__`` protocol, which runs inside
    the caller's event loop. The snapshot loader bridges via
    ``asyncio.run`` internally, which raises ``RuntimeError`` in a
    running loop. ``stream_chat`` catches that as ``Exception`` and
    wraps it as ``ProviderUnavailable``, breaking every streamed call
    for budget tenants. The fix detects a running loop and skips the
    pre-check silently — the async ``ainvoke`` path still enforces the
    cap; streaming soft-degrades (no usage recorded, but the call goes
    through, matching the M4.A spec's documented trade-off).
    """
    primary_provider = MagicMock()

    class _PrefixResolverLike:
        def __call__(self, request):
            return primary_provider

    inner = _PrefixResolverLike()
    snap_cache = MagicMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(hard_cap_tokens=200),
        snapshot_cache=snap_cache,
    )
    # This call MUST NOT raise — the snapshot_cache.get_or_load (which
    # internally calls asyncio.run) must NOT be reached.
    provider = resolver(MagicMock(spec=ChatRequest))
    assert provider is primary_provider
    snap_cache.get_or_load.assert_not_called()


def test_sync_call_outside_async_context_enforces_cap() -> None:
    """``__call__`` from a sync context (no running loop) still enforces.

    The async-context guard must not break the genuine sync path —
    e.g. test fixtures or background threads that construct a
    ``BudgetResolver`` and invoke it without an event loop. The
    ``asyncio.run`` inside ``TenantBudgetSnapshotCache.get_or_load``
    is well-defined in that context, so the pre-check fires as before.
    """
    primary_provider = MagicMock()

    class _PrefixResolverLike:
        def __call__(self, request):
            return primary_provider

    inner = _PrefixResolverLike()
    snap_cache = MagicMock()
    snap_cache.get_or_load = MagicMock(
        return_value=_make_snapshot("t1", "2026-10", 500)
    )
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(hard_cap_tokens=200),
        snapshot_cache=snap_cache,
    )
    # At-cap tenant → TenantBudgetExceeded, inner never called.
    with pytest.raises(TenantBudgetExceeded):
        resolver(MagicMock(spec=ChatRequest))
    snap_cache.get_or_load.assert_called_once()


# ---------------------------------------------------------------------------
# ainvoke_stream — Explore #3 fix: closes the pre-check + post-record
# bypass that ``LLMClient.stream_chat`` previously suffered through the
# loop-running guard in ``_pre_check_sync``.
# ---------------------------------------------------------------------------


def _async_iter(items: list[Any]) -> Any:
    """Return an async iterator yielding ``items`` one at a time."""
    async def _gen() -> Any:
        for item in items:
            yield item
    return _gen()


def _make_streaming_provider(*, items: list[Any]) -> Any:
    """Build a provider-like with a sync ``__call__`` returning self and
    async ``stream()`` yielding the supplied items.

    Note: setting ``p.__call__`` after construction does NOT override a
    MagicMock's callable behavior — the trick is to set
    ``return_value`` on a Mock used AS ``__call__``. We do the simpler
    thing here: build a plain class with the right surface.
    """

    class _Provider:
        name = "fake-stream"

        async def stream(self, request: Any) -> Any:
            for item in items:
                yield item

    return _Provider()


def _make_inner_with_provider(provider: Any) -> Any:
    """Build a resolver-shaped fake whose ``__call__`` returns the provider.

    Plain class (not MagicMock) — MagicMock's instance ``__call__``
    can't be cleanly overridden, which silently breaks the streaming
    seam.
    """

    class _Inner:
        def __call__(self, request: Any) -> Any:
            return provider

        async def ainvoke(self, request: Any) -> Any:
            raise AssertionError(
                "ainvoke_stream must not call inner.ainvoke — "
                "it delegates to provider.stream directly"
            )

    return _Inner()


async def test_ainvoke_stream_pre_check_under_cap_runs_post_record() -> None:
    """Under-cap tenant → pre-check passes, items forwarded, post-record runs."""
    from llm_client.types import ChatResponse as RealChatResponse

    items = [
        "delta-1",
        "delta-2",
        RealChatResponse(
            content="done",
            model="m",
            prompt_tokens=10,
            completion_tokens=5,
            finish_reason="stop",
        ),
    ]
    provider = _make_streaming_provider(items=items)
    inner = _make_inner_with_provider(provider)

    cache = MagicMock()
    repo = MagicMock()
    repo.get_for_tenant_period = AsyncMock(
        return_value=_make_snapshot("t1", "2026-10", 50)
    )
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner,
        tenant_id="t1",
        budget=_make_budget(hard_cap_tokens=200),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )

    out = [item async for item in resolver.ainvoke_stream(MagicMock(spec=ChatRequest))]

    # Items forwarded in order, deltas + final ChatResponse
    assert out[0] == "delta-1"
    assert out[1] == "delta-2"
    assert isinstance(out[2], RealChatResponse)

    # Pre-check + post-record each read the snapshot (pre-check for the
    # cap decision, post-record for the UPSERT — same DB-direct path).
    assert repo.get_for_tenant_period.await_count == 2
    # Post-record wrote the new total (50 + 10 + 5 = 65).
    repo.set_tokens_used.assert_awaited_once()
    last_kwargs = repo.set_tokens_used.await_args.kwargs
    assert last_kwargs["tokens_used"] == 65
    assert last_kwargs["period"] == "2026-10"
    # Cache invalidated so the next pre-check sees the fresh total.
    cache.invalidate.assert_called_once_with("t1", period="2026-10")


async def test_ainvoke_stream_at_cap_raises_before_stream() -> None:
    """At-cap tenant → TenantBudgetExceeded; provider.stream never invoked."""
    provider = _make_streaming_provider(items=[])
    inner = _make_inner_with_provider(provider)

    cache = MagicMock()
    repo = MagicMock()
    # At-cap snapshot — 200 used == hard_cap_tokens.
    repo.get_for_tenant_period = AsyncMock(
        return_value=_make_snapshot("t1", "2026-10", 200)
    )
    resolver = BudgetResolver(
        inner=inner,
        tenant_id="t1",
        budget=_make_budget(hard_cap_tokens=200),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )

    with pytest.raises(TenantBudgetExceeded):
        async for _ in resolver.ainvoke_stream(MagicMock(spec=ChatRequest)):
            pass

    # Inner provider never iterated. set_tokens_used was never called
    # because pre-check blocked the stream before any post-record could
    # fire.
    repo.set_tokens_used.assert_not_called()


async def test_ainvoke_stream_no_budget_is_passthrough() -> None:
    """budget=None → no pre-check, no post-record; items just forwarded."""
    from llm_client.types import ChatResponse as RealChatResponse

    items = ["a", "b", RealChatResponse(
        content="x", model="m",
        prompt_tokens=1, completion_tokens=1, finish_reason="stop",
    )]
    provider = _make_streaming_provider(items=items)
    inner = _make_inner_with_provider(provider)

    cache = MagicMock()
    repo = MagicMock()
    resolver = BudgetResolver(
        inner=inner,
        tenant_id="t1",
        budget=None,  # no enforcement contract
        snapshot_cache=cache,
        snapshot_repo=repo,
    )

    out = [item async for item in resolver.ainvoke_stream(MagicMock(spec=ChatRequest))]

    assert len(out) == 3
    # No DB read (no budget → no enforcement)
    repo.get_for_tenant_period.assert_not_called()
    repo.set_tokens_used.assert_not_called()


async def test_ainvoke_stream_post_record_failure_does_not_abort_stream() -> None:
    """A post-record exception must NOT abort the customer's stream.

    Token consumption already happened at the provider — failing to
    record it is a soft miss on the budget tracking, not a stream
    error. The customer must still see the full response.
    """
    from llm_client.types import ChatResponse as RealChatResponse

    items = [
        "delta",
        RealChatResponse(
            content="done",
            model="m",
            prompt_tokens=4,
            completion_tokens=6,
            finish_reason="stop",
        ),
    ]
    provider = _make_streaming_provider(items=items)
    inner = _make_inner_with_provider(provider)

    cache = MagicMock()
    repo = MagicMock()
    repo.get_for_tenant_period = AsyncMock(
        return_value=_make_snapshot("t1", "2026-10", 10)
    )
    repo.set_tokens_used = AsyncMock(
        side_effect=RuntimeError("simulated DB blip on UPSERT")
    )
    resolver = BudgetResolver(
        inner=inner,
        tenant_id="t1",
        budget=_make_budget(hard_cap_tokens=200),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )

    # Should not raise.
    out = [item async for item in resolver.ainvoke_stream(MagicMock(spec=ChatRequest))]
    assert out[0] == "delta"
    assert isinstance(out[1], RealChatResponse)