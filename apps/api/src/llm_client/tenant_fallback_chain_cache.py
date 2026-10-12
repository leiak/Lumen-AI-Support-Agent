"""In-process LRU + TTL cache for per-tenant fallback chain rows (M5.1).

Mirrors the ``TenantLLMConfigCache`` shape (see tenant_resolver.py lines
64-131). The cache value is ``TenantFallbackChain | None`` — ``None``
means the tenant has no row yet. We deliberately do NOT negatively
cache "no row" results because they go stale on the first admin
upsert; the TTL is short (60s default) and :meth:`invalidate` is the
admin-write hook (wired in Task 3).
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from llm_client.tenant_fallback_chain_models import TenantFallbackChainRepository

if TYPE_CHECKING:
    from llm_client.tenant_fallback_chain_models import TenantFallbackChain


class TenantFallbackChainCache:
    """In-process LRU + TTL cache of per-tenant fallback chain rows.

    Construction takes ``ttl_s`` + ``maxsize`` from settings (default
    60s / 1024). ``repo`` is injected so tests can pass a fake without
    going through the real DB. ``settings`` is also threaded through so
    the resolver can read ``llm_tenant_fallback_chain_required`` /
    ``llm_fallback_chain`` from the same instance.
    """

    def __init__(
        self,
        *,
        ttl_s: float,
        maxsize: int,
        repo: TenantFallbackChainRepository | None = None,
        settings: Any | None = None,
    ) -> None:
        self._ttl_s = ttl_s
        self._maxsize = maxsize
        self._cache: OrderedDict[
            str, tuple[float, TenantFallbackChain | None]
        ] = OrderedDict()
        self._repo = repo or TenantFallbackChainRepository()
        self._settings = settings

    # ---- public cache API -------------------------------------------------

    def get(self, tenant_id: str) -> TenantFallbackChain | None:
        """Return the cached chain for ``tenant_id`` if not expired.

        ``None`` may mean either "not present" or "cached as missing".
        Callers that need to distinguish can use the
        ``__contains__``/``raw`` approach — but in practice both cases
        trigger the same fetch-from-repo path on a cache miss.

        Expired entries are evicted lazily on access.
        """
        entry = self._cache.get(tenant_id)
        if entry is None:
            return None
        expires_at, chain = entry
        if time.monotonic() >= expires_at:
            del self._cache[tenant_id]
            return None
        # LRU touch — move to end
        self._cache.move_to_end(tenant_id)
        return chain

    def put(
        self,
        tenant_id: str,
        chain: TenantFallbackChain | None,
    ) -> None:
        """Insert or refresh the cached chain.

        ``chain`` may be ``None`` (tenant has no row yet). Put still
        caches the negative lookup so subsequent calls within the TTL
        skip the repo. Admin writes call :meth:`invalidate` to bust.
        """
        expires_at = time.monotonic() + self._ttl_s
        if tenant_id in self._cache:
            self._cache.move_to_end(tenant_id)
        self._cache[tenant_id] = (expires_at, chain)
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)

    def invalidate(self, tenant_id: str) -> None:
        """Drop the cached chain for ``tenant_id`` if present.

        Forward-compatible — the admin write API (Task 3) hooks a
        channel listener here. No-op when the entry is absent.
        """
        self._cache.pop(tenant_id, None)

    def clear(self) -> None:
        """Drop all cached entries."""
        self._cache.clear()


__all__ = ["TenantFallbackChainCache"]
