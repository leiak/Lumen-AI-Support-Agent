"""In-process TTL cache for budget pre-check short-circuit.

Complements :class:`TenantBudgetSnapshotCache` (LRU + longer TTL). This
cache holds the *result of the cap check* itself for a few seconds
(``precheck_cache_ttl_seconds``, default 5s), so a hot tenant with no
new ``llm_usage`` rows between turns pays zero DB round-trips for the
pre-check. ``BudgetResolver._post_record`` invalidates the entry on
every successful token consumption, so the cache never serves a
stale "below cap" decision after a real write.

Cardinality: the cache key is ``(tenant_id, period)`` — bounded by
active tenants in the current month (typically < 10k).
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any


class PrecheckCache:
    def __init__(self, *, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._store: dict[tuple[str, str], tuple[float, Any]] = {}

    async def get_or_load(
        self,
        tenant_id: str,
        period: str,
        loader: Callable[[], Awaitable[Any]],
    ) -> Any:
        key = (tenant_id, period)
        now = time.monotonic()
        cached = self._store.get(key)
        if cached is not None:
            if (now - cached[0]) < self._ttl:
                return cached[1]
            # Stale entry — drop it before reloading so the dict doesn't
            # accumulate expired tuples across month boundaries.
            del self._store[key]
        value = await loader()
        self._store[key] = (now, value)
        return value

    async def invalidate(self, tenant_id: str, period: str) -> None:
        self._store.pop((tenant_id, period), None)


__all__ = ["PrecheckCache"]
