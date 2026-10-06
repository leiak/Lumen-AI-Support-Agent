"""Per-model breakdown cache + service for M4.D Pack B #5.

Pack B ships in two layers:

* ``CreditService`` (Task 2) invalidates this cache when a credit grant
  changes the effective cap — so the next breakdown query reflects the
  new cap.
* ``BudgetResolver`` (Task 3) invalidates after each successful
  ``_post_record`` so the next breakdown query reflects new usage.
* ``PerModelService`` + ``BreakdownItem`` (Task 4) consume the cache
  via ``get_breakdown(tenant_id, period)``.

This module ships the cache + singleton getter in Task 2 (so Pack B
resolvers can invalidate it on credit grant + post-record). Task 4
extends the same cache class to be type-tightened on
:class:`ModelUsage`, then adds ``PerModelService`` + the
``ModelUsage`` dataclass alongside the ``?breakdown=true`` admin
endpoint.

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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from llm_client.models import LLMUsage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ModelUsage:
    """One row in the per-model breakdown (Pack B §4.2).

    Aggregates ``llm_usage`` per ``(provider, model)`` group for the
    given period. Frozen so callers can't accidentally mutate the
    cached list — the cache stores ``list[ModelUsage]`` directly and
    relies on the dataclass being immutable.
    """

    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    request_count: int


class PerModelBreakdownCache:
    """In-process dict cache keyed by ``(tenant_id, period)``.

    Stores ``list[ModelUsage]``. Type annotations were tightened from
    ``list[Any]`` to ``list[ModelUsage]`` in Task 4 once ``ModelUsage``
    was added to this same module (no more import-cycle concern).
    """

    def __init__(self, ttl_seconds: int = 30) -> None:
        self._ttl = ttl_seconds
        self._store: dict[tuple[str, str], tuple[float, list[ModelUsage]]] = {}

    def get(self, tenant_id: str, period: str) -> list[ModelUsage] | None:
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

    def set(self, tenant_id: str, period: str, data: list[ModelUsage]) -> None:
        """Insert or refresh the cached breakdown for ``(tenant_id, period)``."""
        self._store[(tenant_id, period)] = (time.monotonic(), data)

    def invalidate(self, tenant_id: str, period: str) -> None:
        """Drop the cached breakdown for ``(tenant_id, period)`` if present."""
        self._store.pop((tenant_id, period), None)


class PerModelService:
    """Read-only per-model breakdown with TTL cache (Pack B §4.2).

    Lazy ``GROUP BY (provider, model)`` on ``llm_usage`` for the given
    period. Cached in-process for 30s (configurable via
    ``PerModelBreakdownCache(ttl_seconds=...)``). Excludes
    ``LLMUsage.cached == True`` rows because cache hits are not
    billable — only fresh-endpoint calls contribute to per-model totals.

    Args:
        session: Async SQLAlchemy session — the service does NOT call
            ``commit()`` itself; the caller owns the transaction
            boundary (matches ``TenantBudgetSnapshotRepository.refresh``
            used in Pack A).
        cache: The breakdown cache singleton (use
            :func:`get_per_model_cache` for the app-wide default).
    """

    def __init__(
        self,
        session: "AsyncSession",
        cache: PerModelBreakdownCache,
    ) -> None:
        self._session = session
        self._cache = cache

    async def get_breakdown(
        self, tenant_id: str, period: str
    ) -> list[ModelUsage]:
        """Return the per-model breakdown for ``(tenant_id, period)``.

        On cache hit: returns the cached ``list[ModelUsage]``. On miss:
        runs the GROUP BY query, materializes the result into
        ``ModelUsage`` rows, caches it, then returns it.

        ``period_start`` is computed as ``YYYY-MM-01T00:00:00Z`` (UTC) —
        matches the per-invocation stamp in ``LLMUsage.created_at``. The
        ``period_anchor_tz`` column on ``tenant_budgets`` is a separate
        concept (it controls the month boundary for cap enforcement);
        per-model breakdowns use UTC month boundaries because the
        raw ``created_at`` is UTC.
        """
        cached = self._cache.get(tenant_id, period)
        if cached is not None:
            return cached
        period_start = datetime.strptime(period + "-01", "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
        result = await self._session.execute(
            select(
                LLMUsage.provider,
                LLMUsage.model,
                func.coalesce(func.sum(LLMUsage.prompt_tokens), 0),
                func.coalesce(func.sum(LLMUsage.completion_tokens), 0),
                func.count(),
            )
            .where(LLMUsage.tenant_id == tenant_id)
            .where(LLMUsage.created_at >= period_start)
            .where(LLMUsage.cached == False)  # noqa: E712 — only billable calls
            .group_by(LLMUsage.provider, LLMUsage.model)
        )
        rows = [
            ModelUsage(
                provider=row.provider,
                model=row.model,
                prompt_tokens=int(row[2] or 0),
                completion_tokens=int(row[3] or 0),
                total_tokens=int(row[2] or 0) + int(row[3] or 0),
                request_count=int(row[4]),
            )
            for row in result
        ]
        self._cache.set(tenant_id, period, rows)
        return rows


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


__all__ = [
    "ModelUsage",
    "PerModelBreakdownCache",
    "PerModelService",
    "get_per_model_cache",
]