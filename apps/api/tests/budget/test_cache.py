"""Tests for TenantBudgetSnapshotCache (LRU + TTL)."""
from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

from budget.cache import TenantBudgetSnapshotCache
from budget.models import TenantBudgetSnapshot


def _make_snapshot(tenant_id: str, period: str, tokens_used: int) -> Any:
    """Build a TenantBudgetSnapshot-like stub (no DB)."""
    snap = MagicMock(spec=TenantBudgetSnapshot)
    snap.tenant_id = tenant_id
    snap.period = period
    snap.tokens_used = tokens_used
    snap.id = "fake-ulid"
    snap.last_refreshed_at = None
    return snap


class _FakeRepo:
    """Fake repo — get_for_tenant_period returns None to force the refresh path.

    The cache logic first tries the snapshot table; if absent, it calls
    refresh. Returning None from get_for_tenant_period ensures refresh
    is exercised in the cache-miss tests below.
    """

    def __init__(self, by_key: dict[tuple[str, str], Any]) -> None:
        self.by_key = by_key
        self.refresh_count = 0

    async def get_for_tenant_period(self, tenant_id: str, period: str) -> Any:
        return None

    async def refresh(self, *, tenant_id: str, period: str, period_starts_at: Any) -> Any:
        self.refresh_count += 1
        return self.by_key[(tenant_id, period)]


def test_cache_hit_skips_refresh() -> None:
    repo = _FakeRepo({("t1", "2026-10"): _make_snapshot("t1", "2026-10", 500)})
    cache = TenantBudgetSnapshotCache(ttl_s=60.0, maxsize=16, repo=repo)
    # Pre-populate cache so get_or_load hits the cache on both calls.
    cache.put("t1", period="2026-10", snap=_make_snapshot("t1", "2026-10", 500))
    snap = cache.get_or_load("t1", period="2026-10", period_starts_at=None)
    snap2 = cache.get_or_load("t1", period="2026-10", period_starts_at=None)
    assert snap.tokens_used == 500
    assert snap2.tokens_used == 500
    assert repo.refresh_count == 0


def test_cache_miss_calls_refresh() -> None:
    repo = _FakeRepo({("t1", "2026-10"): _make_snapshot("t1", "2026-10", 500)})
    cache = TenantBudgetSnapshotCache(ttl_s=60.0, maxsize=16, repo=repo)
    # Force cache miss by populating repo but not cache
    snap = cache.get_or_load("t1", period="2026-10", period_starts_at=None)
    assert snap.tokens_used == 500
    assert repo.refresh_count == 1


def test_cache_respects_ttl() -> None:
    repo = _FakeRepo({("t1", "2026-10"): _make_snapshot("t1", "2026-10", 500)})
    cache = TenantBudgetSnapshotCache(ttl_s=0.01, maxsize=16, repo=repo)
    cache.get_or_load("t1", period="2026-10", period_starts_at=None)
    time.sleep(0.05)  # past TTL
    cache.get_or_load("t1", period="2026-10", period_starts_at=None)
    assert repo.refresh_count == 2


def test_cache_lru_evicts_oldest() -> None:
    repo = _FakeRepo({
        **{(f"t{i}", "2026-10"): _make_snapshot(f"t{i}", "2026-10", 100) for i in range(5)},
    })
    cache = TenantBudgetSnapshotCache(ttl_s=60.0, maxsize=3, repo=repo)
    cache.get_or_load("t0", period="2026-10", period_starts_at=None)
    cache.get_or_load("t1", period="2026-10", period_starts_at=None)
    cache.get_or_load("t2", period="2026-10", period_starts_at=None)
    cache.get_or_load("t3", period="2026-10", period_starts_at=None)  # t0 evicted
    assert cache.get("t0", period="2026-10") is None
    assert cache.get("t1", period="2026-10") is not None
