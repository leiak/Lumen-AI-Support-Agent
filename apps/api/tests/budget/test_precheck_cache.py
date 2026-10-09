"""Tests for PrecheckCache — in-process TTL cache for budget pre-check results.

Complements :class:`TenantBudgetSnapshotCache` (LRU + longer TTL). Holds the
(snapshot, effective_cap) result of the cap decision itself for ~5s so a
hot tenant with no new ``llm_usage`` rows between turns pays zero DB
round-trips for the pre-check.
"""
from __future__ import annotations

import asyncio

import pytest

from budget.precheck_cache import PrecheckCache


@pytest.mark.asyncio
async def test_get_or_load_returns_cached_value_within_ttl() -> None:
    """Within the TTL window, a second call does not call the loader."""
    cache = PrecheckCache(ttl_seconds=5.0)
    calls = 0

    async def loader() -> tuple[str, int]:
        nonlocal calls
        calls += 1
        return ("snap-1", 1000)

    v1 = await cache.get_or_load("tenant-a", "2026-10", loader)
    v2 = await cache.get_or_load("tenant-a", "2026-10", loader)
    assert v1 == v2
    assert calls == 1


@pytest.mark.asyncio
async def test_get_or_load_calls_loader_after_ttl() -> None:
    """After TTL expires, loader runs again."""
    cache = PrecheckCache(ttl_seconds=0.05)  # 50ms
    calls = 0

    async def loader() -> tuple[str, int]:
        nonlocal calls
        calls += 1
        return ("snap", 1000)

    await cache.get_or_load("tenant-a", "2026-10", loader)
    await asyncio.sleep(0.1)
    await cache.get_or_load("tenant-a", "2026-10", loader)
    assert calls == 2


@pytest.mark.asyncio
async def test_invalidate_forces_reload() -> None:
    """invalidate() forces the next call to re-invoke the loader."""
    cache = PrecheckCache(ttl_seconds=60.0)
    calls = 0

    async def loader() -> tuple[str, int]:
        nonlocal calls
        calls += 1
        return ("snap", 1000)

    await cache.get_or_load("tenant-a", "2026-10", loader)
    await cache.invalidate("tenant-a", "2026-10")
    await cache.get_or_load("tenant-a", "2026-10", loader)
    assert calls == 2

