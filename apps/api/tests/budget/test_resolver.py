"""Tests for BudgetResolver pre-check + post-record behavior."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    s.last_refreshed_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
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
    cache.get_or_load_async = AsyncMock(
        return_value=_make_snapshot("t1", "2026-10", 50)
    )
    snapshot_repo = MagicMock()
    snapshot_repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(hard_cap_tokens=200),
        snapshot_cache=cache,
        snapshot_repo=snapshot_repo,
    )
    result = await resolver.ainvoke(MagicMock(spec=ChatRequest))
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 50
    # Inner was called exactly once
    inner.ainvoke.assert_awaited_once()
    # Post-record UPSERTed the snapshot (50 + 150 = 200, just under cap)
    snapshot_repo.set_tokens_used.assert_awaited_once_with(
        tenant_id="t1", period="2026-10", tokens_used=200
    )


async def test_pre_check_raises_when_at_cap() -> None:
    """Snapshot at/above hard cap → TenantBudgetExceeded raised, inner NOT called."""
    inner = MagicMock()
    inner.ainvoke = AsyncMock()
    cache = MagicMock()
    cache.get_or_load_async = AsyncMock(
        return_value=_make_snapshot("t1", "2026-10", 200)
    )
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(hard_cap_tokens=200),
        snapshot_cache=cache,
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
    cache.get_or_load_async = AsyncMock(
        return_value=_make_snapshot("t1", "2026-10", 0)
    )
    repo = MagicMock()
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    repo.set_tokens_used.assert_awaited_once_with(
        tenant_id="t1", period="2026-10", tokens_used=150
    )
    # Cache invalidated with period kwarg (NOT just tenant_id)
    cache.invalidate.assert_called_once_with("t1", period="2026-10")


async def test_post_record_fires_soft_warn_once() -> None:
    """Soft-warn fires exactly once when THIS call crosses soft_warn_tokens.

    Each ``ainvoke`` reads the snapshot twice (pre-check + post-record),
    so the iterator must yield 4 entries — 99, 99 for the first call,
    then 149, 149 for the second. The first call's pre-check + post-
    record both run against snap=99 → fires soft-warn (99 < 100 <= 149).
    The second call's pre-check + post-record both run against snap=149
    → does NOT fire (149 < 100 is False). Net: this test adds +1 to
    the metric counter.

    Counter is a process-level singleton; we measure the delta.
    """
    from core.business_metrics import LLM_TENANT_BUDGET_SOFT_WARN_TOTAL

    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response(prompt_tokens=100, completion_tokens=50))
    cache = MagicMock()

    snap_states = iter(
        [
            _make_snapshot("t1", "2026-10", 99),  # 1st ainvoke: pre-check
            _make_snapshot("t1", "2026-10", 99),  # 1st ainvoke: post-record (cache invalidated)
            _make_snapshot("t1", "2026-10", 149),  # 2nd ainvoke: pre-check (post-UPSERT value)
            _make_snapshot("t1", "2026-10", 149),  # 2nd ainvoke: post-record
        ]
    )
    cache.get_or_load_async = AsyncMock(side_effect=lambda *a, **kw: next(snap_states))
    repo = MagicMock()
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
    # First call crosses threshold (99 < 100 <= 149) → +1
    # Second call does NOT cross (149 < 100 is False) → +0
    assert after - before == 1


async def test_exception_carries_full_context() -> None:
    """TenantBudgetExceeded carries tenant_id, period, tokens_used, hard_cap, period_starts_at."""
    inner = MagicMock()
    inner.ainvoke = AsyncMock()
    cache = MagicMock()
    cache.get_or_load_async = AsyncMock(
        return_value=_make_snapshot("t-xyz", "2026-10", 1000)
    )
    resolver = BudgetResolver(
        inner=inner, tenant_id="t-xyz",
        budget=_make_budget(hard_cap_tokens=500),
        snapshot_cache=cache,
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
    period_utf8, period_start_utc = _current_period("UTC")
    assert period_start_utc.utcoffset() == timedelta(0)


def test_current_period_falls_back_to_utc_on_bad_malformed() -> None:
    """Malformed tz_name does not crash — falls back to UTC."""
    from budget.resolver import _current_period
    # "Not/A/Zone" is not a valid IANA name
    period, period_start = _current_period("Not/A/Zone")
    assert len(period) == 7
    assert period_start.utcoffset() == timedelta(0)  # UTC fallback