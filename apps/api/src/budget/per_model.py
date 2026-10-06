"""Per-model breakdown cache for M4.D Pack B.

Pack B ships in two layers:

* ``CreditService`` (Task 2) invalidates this cache when a credit grant
  changes the effective cap — so the next breakdown query reflects the
  new cap.
* ``BudgetResolver`` (Task 3) invalidates after each successful
  ``_post_record`` so the next breakdown query reflects new usage.
* ``PerModelService`` + ``BreakdownItem`` (Task 4) consume the cache
  via ``get_breakdown(tenant_id, period)``.

This module ships the cache + singleton getter now (Task 2 dependency).
``PerModelService`` + ``ModelUsage`` arrive in Task 4 alongside the
``?breakdown=true`` admin endpoint and the e2e test.

Cache semantics (full design in Pack B spec §4.2):

* Keyed by ``(tenant_id, period)``.
* In-process (NOT Redis-backed) — fine for a single API replica.
* TTL default 30s, configurable via
  ``TENANT_BUDGET_PER_MODEL_CACHE_TTL_SECONDS``.
* ``invalidate(tenant_id, period)`` drops a single entry — used by
  credit grants and post-record writes.
"""
from __future__ import annotations

import time
from typing import Any


class PerModelBreakdownCache:
    """In-process dict cache keyed by ``(tenant_id, period)``.

    Stores ``list[Any]`` rather than ``list[ModelUsage]`` to avoid an
    import cycle with ``ModelUsage`` (which ships in Task 4). Callers
    that write typed ``ModelUsage`` lists will typecheck at runtime.
    """

    def __init__(self, ttl_seconds: int = 30) -> None:
        self._ttl = ttl_seconds
        self._store: dict[tuple[str, str], tuple[float, list[Any]]] = {}

    def get(self, tenant_id: str, period: str) -> list[Any] | None:
        """Return the cached breakdown if present and not expired; else None.

        Expired entries are evicted lazily on access.
        """
        entry = self._store.get((tenant_id, period))
        if entry is None:
            return None
        ts, data = entry
        if time.monotonic() - ts > self._ttl:
            self._store.pop((tenant_id, period), None)
            return None
        return data

    def set(self, tenant_id: str, period: str, data: list[Any]) -> None:
        """Insert or refresh the cached breakdown for ``(tenant_id, period)``."""
        self._store[(tenant_id, period)] = (time.monotonic(), data)

    def invalidate(self, tenant_id: str, period: str) -> None:
        """Drop the cached breakdown for ``(tenant_id, period)`` if present."""
        self._store.pop((tenant_id, period), None)


# Module-level singleton for the FastAPI app.
_default_cache: PerModelBreakdownCache | None = None


def get_per_model_cache(ttl_seconds: int = 30) -> PerModelBreakdownCache:
    """Return the default process-local cache singleton.

    Spec §4.2: the cache is process-local — a single API replica
    reuses the same dict across requests. Multi-replica deployments
    would need a Redis-backed variant; Pack B ships the in-process
    version because that's the project norm (M4.D Pack A's snapshot
    cache is also in-process).
    """
    global _default_cache
    if _default_cache is None:
        _default_cache = PerModelBreakdownCache(ttl_seconds=ttl_seconds)
    return _default_cache


__all__ = ["PerModelBreakdownCache", "get_per_model_cache"]