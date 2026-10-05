"""Per-tenant LLM resolver with in-process LRU + TTL cache.

A :class:`TenantResolver` is built per-tenant on cache miss by:

1. Looking up the tenant's enabled ``TenantLLMConfig`` rows.
2. Decrypting each row's API key with the project Fernet cipher.
3. Constructing a tenant-private :class:`LLMGateway` from those
   providers (which keeps the existing prefix-router / fallback-chain
   logic intact).
4. Returning the gateway's default resolver as the cache value.

Subsequent LLM calls for the same tenant hit the cache and reuse the
resolver. The cache is process-local; multi-instance deployments see
one DB hit per process per tenant per TTL window (60s by default).

There are two build paths:

* :func:`build_tenant_resolver` — async builder; the production entry
  point used by ``_default_llm_client_factory`` (Task 3). It awaits
  the repository call and constructs the resolver in the caller's
  event loop.
* :meth:`TenantLLMConfigCache.get_or_load` — synchronous helper used
  by tests (and simple admin scripts). It bridges to the async
  repository with :func:`asyncio.run` and is **not** callable from
  inside a running event loop.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from llm_client.exceptions import TenantLlmNotConfigured
from llm_client.resolvers import Resolver
from llm_client.tenant_config_models import TenantLLMConfigRepository

if TYPE_CHECKING:
    from llm_client.gateway import LLMGateway
    from llm_client.types import ChatRequest, ChatResponse


class _NoChainConfigured(Exception):
    """Private sentinel: tenant has a single provider, no fallback chain.

    :meth:`TenantResolver.ainvoke` raises when the inner resolver is a
    ``_PrefixResolver`` (single-provider tenants) instead of a
    ``FallbackResolver``. ``LLMClient`` catches this and falls back to
    its own retry loop on the primary provider so the metric path
    stays identical to M4.A.
    """


# Known provider names — matches M4.A's ``_PrefixResolver`` table and
# the spec's §8.4 strict-mode ``missing_providers`` list.
_KNOWN_PROVIDERS: tuple[str, ...] = ("minimax", "anthropic", "openai")


class TenantLLMConfigCache:
    """In-process LRU + TTL cache of per-tenant resolvers.

    Construction takes ``ttl_s`` + ``maxsize`` from settings (default
    60s / 1024). ``repo`` / ``cipher`` / ``settings`` are injected so
    tests can pass fakes without going through the real DB / Fernet /
    Settings stack.
    """

    def __init__(
        self,
        *,
        ttl_s: float,
        maxsize: int,
        repo: TenantLLMConfigRepository | None = None,
        cipher: Any | None = None,
        settings: Any | None = None,
    ) -> None:
        self._ttl_s = ttl_s
        self._maxsize = maxsize
        self._cache: OrderedDict[str, tuple[float, Resolver]] = OrderedDict()
        self._repo = repo or TenantLLMConfigRepository()
        # ``cipher`` and ``settings`` are injected by the factory or
        # by test fixtures so the constructor stays callable without
        # any DB or Fernet state. ``_build_providers`` uses them.
        self._cipher = cipher
        self._settings = settings

    # ---- public cache API -------------------------------------------------

    def get(self, tenant_id: str) -> Resolver | None:
        """Return the cached resolver for ``tenant_id`` if not expired.

        Expired entries are evicted lazily on access.
        """
        entry = self._cache.get(tenant_id)
        if entry is None:
            return None
        expires_at, resolver = entry
        if time.monotonic() >= expires_at:
            del self._cache[tenant_id]
            return None
        # LRU touch — move to end
        self._cache.move_to_end(tenant_id)
        return resolver

    def put(self, tenant_id: str, resolver: Resolver) -> None:
        """Insert or refresh the cached resolver."""
        expires_at = time.monotonic() + self._ttl_s
        if tenant_id in self._cache:
            self._cache.move_to_end(tenant_id)
        self._cache[tenant_id] = (expires_at, resolver)
        # Evict oldest entries past maxsize
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)

    def invalidate(self, tenant_id: str) -> None:
        """Drop the cached resolver for ``tenant_id`` if present.

        Forward-compatible — admin write API can hook a channel listener
        here in a follow-up (see spec §11 tech-debt item #1). The
        current implementation relies on TTL expiry for propagation.
        """
        self._cache.pop(tenant_id, None)

    def clear(self) -> None:
        """Drop all cached entries."""
        self._cache.clear()

    # ---- sync build helper (test path) -----------------------------------

    def get_or_load(self, tenant_id: str) -> Resolver:
        """Return the cached resolver, or build + cache a new one.

        Synchronous wrapper around the async repo. **Not safe to call
        from inside a running event loop** — use
        :func:`build_tenant_resolver` from async call sites.

        Raises:
            TenantLlmNotConfigured: if the tenant has zero enabled
                configs.
        """
        cached = self.get(tenant_id)
        if cached is not None:
            return cached
        # Cache miss path. Bridge to the async repo synchronously.
        rows = asyncio.run(
            self._repo.list_by_tenant(tenant_id, enabled_only=True)
        )
        providers = self._build_providers(rows)
        return self._raise_or_return(tenant_id, providers)

    def _build_providers(self, rows: list[Any]) -> dict[str, Any]:
        """Decrypt each row's API key and instantiate the matching provider.

        Decryption failures raise ``RuntimeError("encrypted_api_key
        corrupted")`` so the operator notices a master-key rotation
        problem — never silently fall back. Unknown provider names are
        silently skipped; they surface as missing in admin API tests.

        Public for mocking in tests; production callers should use
        :meth:`get_or_load` or :func:`build_tenant_resolver`.
        """
        from llm_client.providers.anthropic_provider import AnthropicProvider
        from llm_client.providers.openai_provider import OpenAIProvider

        out: dict[str, Any] = {}
        for row in rows:
            try:
                api_key = self._cipher.decrypt(row.encrypted_api_key)
            except Exception as exc:
                raise RuntimeError(
                    f"encrypted_api_key corrupted for tenant_id={row.tenant_id} "
                    f"provider_name={row.provider_name}: {type(exc).__name__}"
                ) from exc
            if row.provider_name == "minimax":
                out[row.provider_name] = OpenAIProvider(
                    api_key=api_key,
                    model=(
                        self._settings.minimax_model
                        if self._settings is not None
                        else None
                    ) or "MiniMax-M3",
                    base_url=(
                        row.base_url
                        or (
                            self._settings.minimax_base_url
                            if self._settings is not None
                            else None
                        )
                        or "https://api.minimaxi.com/v1"
                    ),
                )
            elif row.provider_name == "anthropic":
                out[row.provider_name] = AnthropicProvider(
                    api_key=api_key,
                    model=(
                        self._settings.default_llm_model
                        if self._settings is not None
                        else "claude-3-5-sonnet-20241022"
                    ),
                )
            elif row.provider_name == "openai":
                out[row.provider_name] = OpenAIProvider(
                    api_key=api_key,
                    model=(
                        self._settings.openai_model
                        if self._settings is not None
                        else None
                    ) or "gpt-4o-mini",
                )
            # Unknown provider_name: silently skip — surfaces in admin API tests
        return out

    def _raise_or_return(
        self,
        tenant_id: str,
        providers: dict[str, Any],
        known_providers: list[str] | None = None,
    ) -> Resolver:
        """Build the inner gateway; raise TenantLlmNotConfigured if empty.

        The ``known_providers`` argument is used only for exception
        construction; the build proceeds whenever ``providers`` has
        at least one entry (the spec's per-request strict mode applies
        later when a request with the wrong model prefix arrives).
        """
        if not providers:
            known = (
                list(known_providers)
                if known_providers is not None
                else list(_KNOWN_PROVIDERS)
            )
            missing = [p for p in known if p not in providers]
            raise TenantLlmNotConfigured(
                tenant_id=tenant_id,
                missing_providers=missing,
            )
        # Lazy imports to avoid circular dep with gateway
        from llm_client.gateway import LLMGateway
        from llm_client.provider_registry import parse_fallback_chain_env

        chain = (
            parse_fallback_chain_env(self._settings.llm_fallback_chain)
            if self._settings is not None
            else None
        )
        gateway = LLMGateway(
            providers=providers,
            default_fallback_chain=chain or None,
            attempt_timeout_s=(
                self._settings.llm_fallback_attempt_timeout_s
                if self._settings is not None
                else None
            ),
        )
        resolver = gateway.default_resolver
        self.put(tenant_id, resolver)
        return resolver


# ---- async production path -----------------------------------------------


async def build_tenant_resolver(
    tenant_id: str,
    *,
    cache: TenantLLMConfigCache,
) -> Resolver:
    """Async builder: return the cached or freshly-built resolver for a tenant.

    Production entry point called from ``_default_llm_client_factory``
    (Task 3) — awaits the repository call and reuses the cache for
    subsequent calls in the same tenant.

    Raises:
        TenantLlmNotConfigured: tenant has no enabled provider configs.
    """
    cached = cache.get(tenant_id)
    if cached is not None:
        return cached
    rows = await cache._repo.list_by_tenant(tenant_id, enabled_only=True)
    providers = cache._build_providers(rows)
    return cache._raise_or_return(tenant_id, providers)


class TenantResolver:
    """Per-tenant resolver that satisfies the M4.A Resolver + M4.B ainvoke seam.

    Built by :func:`build_tenant_resolver` once per (tenant, cache-miss)
    cycle. ``__call__`` returns the inner gateway's primary provider
    (M4.A protocol); ``ainvoke`` delegates to the inner fallback chain
    if present, otherwise raises :class:`_NoChainConfigured` so
    ``LLMClient`` can use its own retry loop on the single provider.
    """

    def __init__(self, *, gateway: "LLMGateway") -> None:
        self._gateway = gateway
        self._delegate = gateway.default_resolver

    def __call__(self, request: "ChatRequest") -> Any:
        return self._delegate(request)

    async def ainvoke(self, request: "ChatRequest") -> "ChatResponse":
        if hasattr(self._delegate, "ainvoke"):
            return await self._delegate.ainvoke(request)
        raise _NoChainConfigured()


__all__ = [
    "TenantLLMConfigCache",
    "TenantResolver",
    "TenantLlmNotConfigured",
    "_NoChainConfigured",
    "build_tenant_resolver",
]