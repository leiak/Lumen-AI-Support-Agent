"""Pack B #5 — PerModelBreakdownCache + PerModelService tests.

Split into two layers:

* Unit tests for ``PerModelBreakdownCache`` (6 tests) — no DB required.
* Integration tests for ``PerModelService`` (3 tests) — requires DB.

The unit tests cover the cache semantics specified in M4.D Pack B §4.2:
hit, miss, invalidate, TTL expiry, tenant isolation, period isolation.

The integration tests verify the GROUP BY (provider, model) query,
cache-hit-within-TTL behavior, and the exclusion of cached rows
(``LLMUsage.cached == True`` rows are not billable).

DB integration tests use the ``db_session`` fixture from
``tests/admin/conftest.py`` (shared singleton — see test_resolver.py
for prior art). Tests 1-6 are pure-Python; tests 7-9 are
``@pytest.mark.integration``.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from budget.per_model import (
    ModelUsage,
    PerModelBreakdownCache,
    PerModelService,
)


# ---------------------------------------------------------------------------
# PerModelBreakdownCache unit tests (no DB).
# ---------------------------------------------------------------------------


def test_get_returns_none_on_miss() -> None:
    """Cache miss on an empty store returns None."""
    cache = PerModelBreakdownCache(ttl_seconds=30)
    assert cache.get("t1", "2026-10") is None


def test_set_then_get_returns_cached() -> None:
    """Set followed by Get within TTL returns the same data."""
    cache = PerModelBreakdownCache(ttl_seconds=30)
    data = [
        ModelUsage(
            provider="openai", model="gpt-4o-mini",
            prompt_tokens=100, completion_tokens=50,
            total_tokens=150, request_count=1,
        )
    ]
    cache.set("t1", "2026-10", data)
    assert cache.get("t1", "2026-10") == data


def test_invalidate_drops_entry() -> None:
    """Invalidate drops the cached entry for the exact (tenant, period)."""
    cache = PerModelBreakdownCache(ttl_seconds=30)
    cache.set("t1", "2026-10", [])
    cache.invalidate("t1", "2026-10")
    assert cache.get("t1", "2026-10") is None


def test_ttl_expiry_evicts_entry() -> None:
    """TTL expiry: entry past tttl_seconds returns None (and is removed)."""
    cache = PerModelBreakdownCache(ttl_seconds=1)
    cache.set("t1", "2026-10", [])
    time.sleep(1.1)
    assert cache.get("t1", "2026-10") is None


def test_different_tenants_isolated() -> None:
    """Invalidating (t1, period) does not drop (t2, period)."""
    cache = PerModelBreakdownCache(ttl_seconds=30)
    cache.set("t1", "2026-10", [ModelUsage("a", "b", 0, 0, 0, 0)])
    cache.set("t2", "2026-10", [ModelUsage("c", "d", 0, 0, 0, 0)])
    cache.invalidate("t1", "2026-10")
    assert cache.get("t1", "2026-10") is None
    # t2's entry survives.
    assert cache.get("t2", "2026-10") is not None


def test_different_periods_isolated() -> None:
    """Invalidating (t1, 2026-10) does not drop (t1, 2026-11)."""
    cache = PerModelBreakdownCache(ttl_seconds=30)
    cache.set("t1", "2026-10", [])
    cache.set("t1", "2026-11", [])
    cache.invalidate("t1", "2026-10")
    assert cache.get("t1", "2026-10") is None
    # November's entry survives.
    assert cache.get("t1", "2026-11") is not None


def test_get_per_model_cache_reads_ttl_from_settings(monkeypatch) -> None:
    """Pack B #5: get_per_model_cache() honors
    TENANT_BUDGET_PER_MODEL_CACHE_TTL_SECONDS on first construction.

    Regression test for the dead-config bug caught by the Pack B final
    review: the setting was declared but never consumed — every call
    site used the hardcoded 30s default.
    """
    from core.config import get_settings, reset_settings
    from budget.per_model import (
        get_per_model_cache,
        reset_per_model_cache,
    )

    monkeypatch.setenv("TENANT_BUDGET_PER_MODEL_CACHE_TTL_SECONDS", "7")
    reset_settings()
    reset_per_model_cache()
    try:
        cache = get_per_model_cache()
        assert cache._ttl == 7   # type: ignore[attr-defined] — internal attr
    finally:
        reset_per_model_cache()
        reset_settings()


def test_reset_per_model_cache_drops_singleton() -> None:
    """Pack B #5: reset_per_model_cache() drops the singleton so the
    next get_per_model_cache() constructs a fresh one."""
    from budget.per_model import (
        PerModelBreakdownCache,
        get_per_model_cache,
        reset_per_model_cache,
    )

    cache1 = get_per_model_cache()
    assert isinstance(cache1, PerModelBreakdownCache)
    reset_per_model_cache()
    cache2 = get_per_model_cache()
    # A fresh instance after reset (singleton was dropped).
    assert cache2 is not cache1
    reset_per_model_cache()


# ---------------------------------------------------------------------------
# PerModelService integration tests (DB required).
# ---------------------------------------------------------------------------


async def _seed_usage(
    session: object,
    *,
    tenant_id: str,
    provider: str,
    model: str,
    prompt: int,
    completion: int,
    created_at: datetime,
    cached: bool = False,
) -> None:
    """Insert a single LLMUsage row. Mirrors the project's row-construction style.

    ``id`` is a fresh ULID per call; ULID generation uses ``core.id_gen.new_id``
    (the canonical helper used by every other seed in the project).
    """
    from llm_client.models import LLMUsage
    from core.id_gen import new_id

    row = LLMUsage(
        id=new_id(),
        tenant_id=tenant_id,
        provider=provider,
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=0.0,
        request_id="test-request-" + new_id(),
        cached=cached,
        metadata_json={},
        created_at=created_at,
    )
    session.add(row)  # type: ignore[attr-defined]
    await session.flush()  # type: ignore[attr-defined]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_breakdown_groups_by_provider_model(db_session: object) -> None:
    """GROUP BY (provider, model) sums prompt/completion tokens per group."""
    period_start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    await _seed_usage(
        db_session, tenant_id="t1", provider="openai", model="gpt-4o-mini",
        prompt=100, completion=50, created_at=period_start,
    )
    await _seed_usage(
        db_session, tenant_id="t1", provider="openai", model="gpt-4o-mini",
        prompt=200, completion=100, created_at=period_start,
    )
    await _seed_usage(
        db_session, tenant_id="t1", provider="anthropic", model="haiku",
        prompt=50, completion=25, created_at=period_start,
    )
    cache = PerModelBreakdownCache(ttl_seconds=30)
    svc = PerModelService(db_session, cache)  # type: ignore[arg-type]
    rows = await svc.get_breakdown("t1", "2026-10")
    assert {r.provider for r in rows} == {"openai", "anthropic"}
    openai = next(r for r in rows if r.provider == "openai")
    assert openai.model == "gpt-4o-mini"
    assert openai.prompt_tokens == 300
    assert openai.completion_tokens == 150
    assert openai.total_tokens == 450
    assert openai.request_count == 2
    anthropic = next(r for r in rows if r.provider == "anthropic")
    assert anthropic.model == "haiku"
    assert anthropic.prompt_tokens == 50
    assert anthropic.completion_tokens == 25
    assert anthropic.total_tokens == 75
    assert anthropic.request_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_breakdown_caches_within_ttl(db_session: object) -> None:
    """Second call within TTL returns cached value (new rows invisible)."""
    period_start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    await _seed_usage(
        db_session, tenant_id="t1", provider="openai", model="gpt-4o",
        prompt=10, completion=5, created_at=period_start,
    )
    cache = PerModelBreakdownCache(ttl_seconds=30)
    svc = PerModelService(db_session, cache)  # type: ignore[arg-type]
    first = await svc.get_breakdown("t1", "2026-10")
    assert len(first) == 1
    assert first[0].prompt_tokens == 10
    # Add another row — should NOT be visible (cached within TTL).
    await _seed_usage(
        db_session, tenant_id="t1", provider="openai", model="gpt-4o",
        prompt=999, completion=999, created_at=period_start,
    )
    second = await svc.get_breakdown("t1", "2026-10")
    assert second[0].prompt_tokens == 10  # cached, not refreshed
    # Invalidate → next call sees fresh data.
    cache.invalidate("t1", "2026-10")
    third = await svc.get_breakdown("t1", "2026-10")
    assert third[0].prompt_tokens == 1009


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_breakdown_excludes_cached_rows(db_session: object) -> None:
    """LLMUsage.cached=True rows are excluded (cache hits aren't billable)."""
    period_start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    await _seed_usage(
        db_session, tenant_id="t1", provider="openai", model="gpt-4o-mini",
        prompt=100, completion=50, created_at=period_start, cached=True,
    )
    cache = PerModelBreakdownCache(ttl_seconds=30)
    svc = PerModelService(db_session, cache)  # type: ignore[arg-type]
    rows = await svc.get_breakdown("t1", "2026-10")
    assert rows == []


__all__ = [
    "test_get_returns_none_on_miss",
    "test_set_then_get_returns_cached",
    "test_invalidate_drops_entry",
    "test_ttl_expiry_evicts_entry",
    "test_different_tenants_isolated",
    "test_different_periods_isolated",
    "test_get_breakdown_groups_by_provider_model",
    "test_get_breakdown_caches_within_ttl",
    "test_get_breakdown_excludes_cached_rows",
]