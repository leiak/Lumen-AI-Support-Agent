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

M5.1 also threads the per-tenant fallback chain through this cache:
:TenantLLMConfigCache._fallback_chain_cache` stores per-tenant
``TenantFallbackChain`` rows with the same LRU+TTL shape; the
async build path awaits :meth:`_resolve_chain_steps` before
constructing the inner gateway so the chain honours whichever source
wins (per-tenant row → env chain → strict-mode raise).
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from core.business_metrics import LLM_TENANT_FALLBACK_CHAIN_TOTAL
from llm_client.exceptions import TenantLlmNotConfigured
from llm_client.resolvers import Resolver
from llm_client.tenant_config_models import TenantLLMConfigRepository

if TYPE_CHECKING:
    from llm_client.gateway import LLMGateway
    from llm_client.tenant_fallback_chain_cache import TenantFallbackChainCache
    from llm_client.types import ChatRequest, ChatResponse


class _NoChainConfigured(Exception):  # noqa: N818
    """Internal-but-exported sentinel: tenant has a single provider, no fallback chain.

    :meth:`TenantResolver.ainvoke` raises when the inner resolver is a
    ``_PrefixResolver`` (single-provider tenants) instead of a
    ``FallbackResolver``. ``LLMClient`` catches this and falls back to
    its own retry loop on the primary provider so the metric path
    stays identical to M4.A.

    The leading underscore signals module-private, but the class is
    re-exported in ``__all__`` so :class:`LLMClient` can catch it
    without re-defining the sentinel. This is intentional cross-module
    coupling per spec §8.1 — :class:`LLMClient` is the only consumer.
    """


# Known provider names — matches M4.A's ``_PrefixResolver`` table and
# the spec's §8.4 strict-mode ``missing_providers`` list.
_KNOWN_PROVIDERS: tuple[str, ...] = ("minimax", "anthropic", "openai")


class TenantLLMConfigCache:
    """In-process LRU + TTL cache of per-tenant resolvers.

    Construction takes ``ttl_s`` + ``maxsize`` from settings (default
    60s / 1024). ``repo`` / ``cipher`` / ``settings`` /
    ``fallback_chain_cache`` are injected so tests can pass fakes
    without going through the real DB / Fernet / Settings stack.
    """

    def __init__(
        self,
        *,
        ttl_s: float,
        maxsize: int,
        repo: TenantLLMConfigRepository | None = None,
        cipher: Any | None = None,
        settings: Any | None = None,
        fallback_chain_cache: TenantFallbackChainCache | None = None,
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
        # Lazy import inside the constructor body is unnecessary here —
        # the cache module is a thin leaf that imports only the models
        # file, no circular-dep risk at import time. We still defer the
        # *construction* so callers can pass a fully-configured cache
        # (with their own repo/settings) via ``fallback_chain_cache``.
        if fallback_chain_cache is None:
            from llm_client.tenant_fallback_chain_cache import (
                TenantFallbackChainCache,
            )

            fallback_chain_cache = TenantFallbackChainCache(
                ttl_s=(
                    settings.llm_tenant_fallback_chain_ttl_s
                    if settings is not None
                    else 60.0
                ),
                maxsize=(
                    settings.llm_tenant_fallback_chain_cache_maxsize
                    if settings is not None
                    else 1024
                ),
                settings=settings,
            )
        self._fallback_chain_cache = fallback_chain_cache

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
        # Sync test path: derive the env chain inline (test fixtures
        # don't await ``_resolve_chain_steps``). Production always
        # calls ``build_tenant_resolver`` which awaits it. We pass
        # ``rows`` through so M5.2 can build the per-tenant model
        # override dict (see ``_raise_or_return``).
        return self._raise_or_return(tenant_id, providers, rows=rows)

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
                    # Thread the per-row base_url so a tenant can reroute
                    # the Anthropic-format POST to a third-party proxy
                    # (e.g. Claude Code's MiniMax proxy at
                    # https://api.minimaxi.com/anthropic). When the row
                    # has no base_url we deliberately omit the kwarg so
                    # the provider falls back to api.anthropic.com —
                    # mirrors provider_registry.py's conditional-kwarg
                    # pattern that protects real-Anthropic callers.
                    **({"base_url": row.base_url} if row.base_url else {}),
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

    async def _resolve_chain_steps(
        self,
        tenant_id: str,
    ) -> list[tuple[str, str]]:
        """Return the fallback chain steps for this tenant.

        Order of precedence:
          1. Per-tenant row from cache (or repo on miss), if enabled
             and has at least one enabled step.
          2. Env chain (``RuntimeChain`` singleton — seeded lazily from
             ``settings.llm_fallback_chain`` on first read).
          3. Strict mode raises ``TenantFallbackChainNotConfigured``
             when both sources are empty AND
             ``LLM_TENANT_FALLBACK_CHAIN_REQUIRED`` is True.

        Increments ``LLM_TENANT_FALLBACK_CHAIN_TOTAL{outcome}`` based
        on the outcome so a dashboard can split "tenant explicitly
        configured" from "fell back to env" without per-tenant labels.
        """
        from llm_client.provider_registry import parse_fallback_chain_env
        from llm_client.runtime_chain import get_runtime_chain

        # 1. Per-tenant row.
        chain_row = self._fallback_chain_cache.get(tenant_id)
        if chain_row is None:
            # Cache miss — fetch from repo. Tolerate transient DB
            # errors by treating them as "no row" rather than
            # 500-ing the call site; the env chain below covers the
            # gap.
            try:
                chain_row = await self._fallback_chain_cache._repo.get(
                    tenant_id,
                )
            except Exception:
                chain_row = None
            # Cache the result (None included) so subsequent lookups
            # within the TTL skip the DB round-trip. Admin writes
            # call ``fallback_chain_cache.invalidate``.
            self._fallback_chain_cache.put(tenant_id, chain_row)

        tenant_steps: list[tuple[str, str]] | None = None
        if chain_row is not None and chain_row.enabled:
            enabled_steps = [
                s for s in (chain_row.steps or []) if s.get("enabled", True)
            ]
            if enabled_steps:
                tenant_steps = [
                    (s["provider"], s["model"]) for s in enabled_steps
                ]
                LLM_TENANT_FALLBACK_CHAIN_TOTAL.labels(outcome="hit").inc()
            else:
                LLM_TENANT_FALLBACK_CHAIN_TOTAL.labels(outcome="disabled").inc()
        elif chain_row is not None:
            LLM_TENANT_FALLBACK_CHAIN_TOTAL.labels(outcome="disabled").inc()
        else:
            LLM_TENANT_FALLBACK_CHAIN_TOTAL.labels(outcome="miss").inc()

        if tenant_steps is not None:
            return tenant_steps

        # 2. Env chain fallback.
        runtime = get_runtime_chain()
        if self._settings is not None and not runtime.snapshot():
            env_steps = parse_fallback_chain_env(
                getattr(self._settings, "llm_fallback_chain", None),
            )
            if env_steps:
                runtime.initial_steps(env_steps)
        env_chain_steps = runtime.active_chain() or None
        if env_chain_steps:
            return env_chain_steps

        # 3. Strict mode.
        if self._settings is not None and getattr(
            self._settings,
            "llm_tenant_fallback_chain_required",
            False,
        ):
            from llm_client.exceptions import TenantFallbackChainNotConfigured

            LLM_TENANT_FALLBACK_CHAIN_TOTAL.labels(
                outcome="unconfigured",
            ).inc()
            raise TenantFallbackChainNotConfigured(tenant_id=tenant_id)

        return []

    def _raise_or_return(
        self,
        tenant_id: str,
        providers: dict[str, Any],
        known_providers: list[str] | None = None,
        *,
        chain_steps: list[tuple[str, str]] | None = None,
        rows: list[Any] | None = None,
    ) -> Resolver:
        """Build the inner gateway; raise TenantLlmNotConfigured if empty.

        ``chain_steps`` is the pre-computed fallback chain from
        :meth:`_resolve_chain_steps`. When ``None`` (the default,
        used by the synchronous test path), the env chain is
        computed inline via the same logic; production callers
        always pass a pre-resolved value so the chain policy (per-tenant
        → env → strict-mode raise) is honoured.

        ``rows`` is the (already-fetched) list of ``TenantLLMConfig``
        rows for this tenant. M5.2 uses it to build the per-tenant
        model-override dict so the wrap can rewrite ``request.model``
        to the tenant preference (see below). Re-fetching would
        double the DB hit, so callers pass the list they already
        iterated over to build ``providers``.

        M5.2: when any enabled row in ``rows`` has a non-null ``model``
        column, the gateway resolver is wrapped with
        :class:`TenantModelOverrideResolver` to rewrite ``request.model``
        to the tenant override before the provider receives it. The
        wrap is conditional — empty ``overrides`` dict (no row had a
        non-null ``model``) preserves the pre-M5.2 behavior, so
        existing tenants see zero overhead.

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
        from llm_client.runtime_chain import get_runtime_chain

        # Seed the runtime chain from env once (idempotent). After
        # that, admin mutations via ``ReplaceStore`` win. This is the
        # single source of truth for the default fallback resolver —
        # Task 3.4.
        runtime = get_runtime_chain()
        if self._settings is not None and not runtime.snapshot():
            env_steps = parse_fallback_chain_env(
                self._settings.llm_fallback_chain
            )
            if env_steps:
                runtime.initial_steps(env_steps)
        chain = (
            chain_steps
            if chain_steps is not None
            else (runtime.active_chain() or None)
        )
        gateway = LLMGateway(
            providers=providers,
            default_fallback_chain=chain,
            attempt_timeout_s=(
                self._settings.llm_fallback_attempt_timeout_s
                if self._settings is not None
                else None
            ),
        )
        resolver = gateway.default_resolver
        # M5.2 — wrap the gateway resolver with TenantModelOverrideResolver
        # when any enabled row has a non-null ``model`` column. The wrapper
        # rewrites ``request.model`` AFTER the inner resolver picks the
        # provider (so prefix-match routing still works) but BEFORE the
        # provider receives the request (so the provider's request.model
        # line sees the override). Empty ``overrides`` → no wrap,
        # preserving back-compat for tenants that never set a model.
        if rows is not None:
            overrides: dict[str, str | None] = {
                row.provider_name: row.model
                for row in rows
                if row.enabled and row.model is not None
            }
            if overrides:
                from llm_client.tenant_model_override_resolver import (
                    TenantModelOverrideResolver,
                )
                resolver = TenantModelOverrideResolver(
                    inner=resolver,
                    overrides=overrides,
                )
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

    On cache miss the per-tenant chain is awaited via
    :meth:`TenantLLMConfigCache._resolve_chain_steps` BEFORE the inner
    gateway is built, so the chain honours whichever source wins
    (per-tenant row → env chain → strict-mode raise).

    Raises:
        TenantLlmNotConfigured: tenant has no enabled provider configs.
        TenantFallbackChainNotConfigured: strict mode on, no chain
            available from any source (per-tenant row or env).
    """
    cached = cache.get(tenant_id)
    if cached is not None:
        return cached
    rows = await cache._repo.list_by_tenant(tenant_id, enabled_only=True)
    providers = cache._build_providers(rows)
    chain_steps = await cache._resolve_chain_steps(tenant_id)
    return cache._raise_or_return(
        tenant_id,
        providers,
        chain_steps=chain_steps,
        rows=rows,
    )


class TenantResolver:
    """Per-tenant resolver that satisfies the M4.A Resolver + M4.B ainvoke seam.

    Built by :func:`build_tenant_resolver` once per (tenant, cache-miss)
    cycle. ``__call__`` returns the inner gateway's primary provider
    (M4.A protocol); ``ainvoke`` delegates to the inner fallback chain
    if present, otherwise raises :class:`_NoChainConfigured` so
    ``LLMClient`` can use its own retry loop on the single provider.
    """

    def __init__(self, *, gateway: LLMGateway) -> None:
        self._gateway = gateway
        self._delegate = gateway.default_resolver

    def __call__(self, request: ChatRequest) -> Any:
        return self._delegate(request)

    async def ainvoke(self, request: ChatRequest) -> ChatResponse:
        if hasattr(self._delegate, "ainvoke"):
            return await self._delegate.ainvoke(request)
        raise _NoChainConfigured()


__all__ = [
    "TenantLLMConfigCache",
    "TenantLlmNotConfigured",
    "TenantResolver",
    "_NoChainConfigured",
    "build_tenant_resolver",
]