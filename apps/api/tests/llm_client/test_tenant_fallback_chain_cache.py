"""Tests for TenantFallbackChainCache — pure-cache unit tests (no DB).

Mirrors ``test_tenant_resolver.py`` patterns:
* sync ``def`` tests (no async; the cache itself is sync)
* ``_FakeRepo`` for repo injection; counters ``call_count``
* no ``@pytest.mark.integration`` marker — these are unit tests

Each test exercises a single cache method and asserts on observable
state. The cache value type (``TenantFallbackChain | None``) lets us
distinguish "no row" (``None``) from "row absent from cache" (also
``None`` after a miss) — both collapse to ``None`` on ``get``;
``__contains__`` (via the internal ``_cache`` mapping) is sufficient
for distinguishing if needed.
"""
from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

from llm_client.tenant_fallback_chain_cache import TenantFallbackChainCache


class _FakeRepo:
    """Repo fake — exposes the async ``get`` and counts calls."""

    def __init__(self, rows_by_tenant: dict[str, Any]) -> None:
        self.rows_by_tenant = rows_by_tenant
        self.call_count = 0

    async def get(self, tenant_id: str) -> Any:
        self.call_count += 1
        return self.rows_by_tenant.get(tenant_id)


def _make_chain_row(tenant_id: str, *, enabled: bool = True) -> Any:
    """Return a MagicMock that quacks like ``TenantFallbackChain``."""
    row = MagicMock()
    row.tenant_id = tenant_id
    row.enabled = enabled
    row.steps = [
        {"provider": "anthropic", "model": "claude-haiku-4-5", "enabled": True},
    ]
    return row


def test_get_returns_none_on_miss() -> None:
    cache = TenantFallbackChainCache(
        ttl_s=60.0,
        maxsize=16,
        repo=_FakeRepo({}),  # type: ignore[arg-type]
    )
    assert cache.get("t1") is None


def test_put_then_get_returns_chain() -> None:
    cache = TenantFallbackChainCache(
        ttl_s=60.0,
        maxsize=16,
        repo=_FakeRepo({}),  # type: ignore[arg-type]
    )
    chain = _make_chain_row("t1")
    cache.put("t1", chain)
    assert cache.get("t1") is chain


def test_put_none_caches_negative() -> None:
    """``put(None)`` records a negative lookup — subsequent ``get`` returns None
    without re-fetching. Admin writes use ``invalidate`` to bust."""
    cache = TenantFallbackChainCache(
        ttl_s=60.0,
        maxsize=16,
        repo=_FakeRepo({}),  # type: ignore[arg-type]
    )
    cache.put("t1", None)
    assert cache.get("t1") is None
    # Re-put the same key to ensure idempotency.
    cache.put("t1", None)
    assert cache.get("t1") is None


def test_ttl_expiry() -> None:
    cache = TenantFallbackChainCache(
        ttl_s=0.01,
        maxsize=16,
        repo=_FakeRepo({}),  # type: ignore[arg-type]
    )
    chain = _make_chain_row("t1")
    cache.put("t1", chain)
    assert cache.get("t1") is chain
    time.sleep(0.05)  # past TTL
    assert cache.get("t1") is None


def test_lru_eviction() -> None:
    """``maxsize=3`` evicts the oldest tenant on the 4th insert."""
    cache = TenantFallbackChainCache(
        ttl_s=60.0,
        maxsize=3,
        repo=_FakeRepo({}),  # type: ignore[arg-type]
    )
    rows = {f"t{i}": _make_chain_row(f"t{i}") for i in range(4)}
    for i in range(4):
        cache.put(f"t{i}", rows[f"t{i}"])
    # t0 evicted (oldest); t1/t2/t3 still present.
    assert cache.get("t0") is None
    assert cache.get("t1") is rows["t1"]
    assert cache.get("t2") is rows["t2"]
    assert cache.get("t3") is rows["t3"]


def test_invalidate_removes_entry() -> None:
    cache = TenantFallbackChainCache(
        ttl_s=60.0,
        maxsize=16,
        repo=_FakeRepo({}),  # type: ignore[arg-type]
    )
    chain = _make_chain_row("t1")
    cache.put("t1", chain)
    assert cache.get("t1") is chain
    cache.invalidate("t1")
    assert cache.get("t1") is None


def test_invalidate_no_op_when_absent() -> None:
    cache = TenantFallbackChainCache(
        ttl_s=60.0,
        maxsize=16,
        repo=_FakeRepo({}),  # type: ignore[arg-type]
    )
    # Must not raise — invalidate is idempotent.
    cache.invalidate("ghost-tenant")
    assert cache.get("ghost-tenant") is None


def test_clear_drops_all() -> None:
    cache = TenantFallbackChainCache(
        ttl_s=60.0,
        maxsize=16,
        repo=_FakeRepo({}),  # type: ignore[arg-type]
    )
    for i in range(3):
        cache.put(f"t{i}", _make_chain_row(f"t{i}"))
    assert len(cache._cache) == 3  # type: ignore[attr-defined]
    cache.clear()
    assert len(cache._cache) == 0  # type: ignore[attr-defined]
    for i in range(3):
        assert cache.get(f"t{i}") is None


def test_constructor_without_settings_uses_defaults() -> None:
    """When ``settings`` is omitted the cache should fall back to the
    M4.C-shaped defaults (60s / 1024). Mirrors the no-settings test
    pattern in test_tenant_resolver.py.
    """
    cache = TenantFallbackChainCache(
        ttl_s=60.0,
        maxsize=1024,
        repo=_FakeRepo({}),  # type: ignore[arg-type]
    )
    assert cache._ttl_s == 60.0  # type: ignore[attr-defined]
    assert cache._maxsize == 1024  # type: ignore[attr-defined]
    assert cache._settings is None  # type: ignore[attr-defined]


def test_repo_passed_through() -> None:
    """The constructor honours an injected repo so the cache module
    never depends on a real DB session.
    """
    fake_repo = _FakeRepo({})  # type: ignore[arg-type]
    cache = TenantFallbackChainCache(
        ttl_s=60.0,
        maxsize=16,
        repo=fake_repo,  # type: ignore[arg-type]
    )
    assert cache._repo is fake_repo  # type: ignore[attr-defined]
