"""In-process LRU + TTL cache of per-(tenant, period) budget snapshots.

Mirrors M4.C's :class:`TenantLLMConfigCache`. Cache key is
``(tenant_id, period)`` so the same tenant has separate entries across
month boundaries.

On cache miss:
1. Try ``repo.get_for_tenant_period`` — if a row exists, return it.
2. Else call ``repo.refresh`` (which runs ``SUM(llm_usage)`` and writes
   the snapshot row).

Within the TTL window, subsequent reads are pure memory hits.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from datetime import datetime

from budget.models import TenantBudgetSnapshot
from budget.repository import TenantBudgetSnapshotRepository


class TenantBudgetSnapshotCache:
    """In-process LRU + TTL cache keyed by ``(tenant_id, period)``."""

    def __init__(
        self,
        *,
        ttl_s: float,
        maxsize: int,
        repo: TenantBudgetSnapshotRepository | None = None,
    ) -> None:
        self._ttl_s = ttl_s
        self._maxsize = maxsize
        self._cache: OrderedDict[tuple[str, str], tuple[float, TenantBudgetSnapshot]] = OrderedDict()
        self._repo = repo or TenantBudgetSnapshotRepository()

    def get(
        self, tenant_id: str, *, period: str
    ) -> TenantBudgetSnapshot | None:
        """Return the cached snapshot if present and not expired; else None.

        Expired entries are evicted lazily on access.
        """
        entry = self._cache.get((tenant_id, period))
        if entry is None:
            return None
        expires_at, snap = entry
        if time.monotonic() >= expires_at:
            del self._cache[(tenant_id, period)]
            return None
        # LRU touch — move to end
        self._cache.move_to_end((tenant_id, period))
        return snap

    def put(
        self, tenant_id: str, *, period: str, snap: TenantBudgetSnapshot
    ) -> None:
        """Insert or refresh the cached snapshot; evict oldest past maxsize."""
        expires_at = time.monotonic() + self._ttl_s
        key = (tenant_id, period)
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = (expires_at, snap)
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)

    def invalidate(self, tenant_id: str, *, period: str) -> None:
        """Drop the cached snapshot for (tenant_id, period) if present."""
        self._cache.pop((tenant_id, period), None)

    def clear(self) -> None:
        """Drop all cached entries."""
        self._cache.clear()

    def get_or_load(
        self,
        tenant_id: str,
        *,
        period: str,
        period_starts_at: datetime,
    ) -> TenantBudgetSnapshot:
        """Return the cached snapshot or build + cache a new one.

        Synchronous wrapper around the async repo. **Not safe to call
        from inside a running event loop** — production code should use
        :meth:`get_or_load_async` from async call sites (Task 3 resolver).
        """
        cached = self.get(tenant_id, period=period)
        if cached is not None:
            return cached
        # Cache miss: try snapshot table first; if absent, refresh via SUM
        snap = asyncio.run(
            self._repo.get_for_tenant_period(tenant_id, period)
        )
        if snap is None:
            snap = asyncio.run(
                self._repo.refresh(
                    tenant_id=tenant_id,
                    period=period,
                    period_starts_at=period_starts_at,
                )
            )
        self.put(tenant_id, period=period, snap=snap)
        return snap

    async def get_or_load_async(
        self,
        tenant_id: str,
        *,
        period: str,
        period_starts_at: datetime,
    ) -> TenantBudgetSnapshot:
        """Async cache-or-load: cached snapshot OR repo.get_for_tenant_period OR repo.refresh.

        Production entry point used by :class:`BudgetResolver` (Task 3).
        Awaits the repository call directly without asyncio.run.
        """
        cached = self.get(tenant_id, period=period)
        if cached is not None:
            return cached
        snap = await self._repo.get_for_tenant_period(tenant_id, period)
        if snap is None:
            snap = await self._repo.refresh(
                tenant_id=tenant_id,
                period=period,
                period_starts_at=period_starts_at,
            )
        self.put(tenant_id, period=period, snap=snap)
        return snap


__all__ = ["TenantBudgetSnapshotCache"]
