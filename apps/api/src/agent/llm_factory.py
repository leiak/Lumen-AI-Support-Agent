"""Default LLMClient factory — builds tenant-scoped LLMClient instances
backed by a per-tenant LLMGateway via BYOK config (M4.C), optionally
composed with a :class:`BudgetResolver` (M4.D) when the tenant has a
``tenant_budgets`` row.

Order of composition when budget is configured:
    LLMClient → BudgetResolver → TenantResolver → FallbackResolver → Provider

Tenants without a ``tenant_budgets`` row are NOT wrapped — M4.D is
opt-in per tenant. Existing M4.C strict-mode behavior
(:class:`TenantLlmNotConfigured`) is preserved.

PII discipline: error paths carry opaque IDs (``tenant_id``) and
counters; never message content or API keys.
"""
from __future__ import annotations

from budget.cache import TenantBudgetSnapshotCache
from budget.precheck_cache import PrecheckCache
from budget.repository import (
    TenantBudgetRepository,
    TenantBudgetSnapshotRepository,
)
from budget.resolver import BudgetResolver
from core.business_metrics import LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL
from core.config import get_settings
from llm_client.client import LLMClient
from llm_client.exceptions import TenantLlmNotConfigured
from llm_client.http_pool import HttpClientPool
from llm_client.tenant_config_crypto import TenantLLMConfigCipher
from llm_client.tenant_config_models import TenantLLMConfigRepository
from llm_client.tenant_resolver import (
    TenantLLMConfigCache,
    build_tenant_resolver,
)
from llm_client.usage import UsageRecorder

# Module-level singletons (per process). Tests reset via
# ``agent.llm_factory._tenant_cache = None`` or
# ``agent.llm_factory._budget_snapshot_cache = None`` when they need a
# fresh cipher / cache (e.g. after monkeypatching env).
_tenant_cache: TenantLLMConfigCache | None = None
_budget_snapshot_cache: TenantBudgetSnapshotCache | None = None
_precheck_cache: PrecheckCache | None = None

# Process-wide HTTP pool (nitpick S4 / tech-debt #24). Shared across
# all LLM provider instances so the HTTPS connection pool + TLS
# session are paid once per (base_url, headers) for the process
# lifetime, not per turn.
_http_pool: HttpClientPool | None = None


def _build_tenant_cache() -> TenantLLMConfigCache:
    """Construct the per-process ``TenantLLMConfigCache`` singleton.

    Lazy because ``TenantLLMConfigCipher.__init__`` raises if the
    Fernet key is unset — we don't want to crash import-time on a
    dev / CI environment that never hits the LLM path. The first
    :func:`_default_llm_client_factory` call pays the construction
    cost; subsequent calls reuse the cached instance.
    """
    global _tenant_cache
    if _tenant_cache is None:
        settings = get_settings()
        cipher = TenantLLMConfigCipher(settings.tenant_llm_fernet_key)
        _tenant_cache = TenantLLMConfigCache(
            ttl_s=settings.tenant_llm_cache_ttl_s,
            maxsize=settings.tenant_llm_cache_maxsize,
            repo=TenantLLMConfigRepository(),
            cipher=cipher,
            settings=settings,
        )
    return _tenant_cache


def _build_budget_snapshot_cache() -> TenantBudgetSnapshotCache:
    """Construct the per-process ``TenantBudgetSnapshotCache`` singleton.

    Mirrors :func:`_build_tenant_cache`. Reads TTL / maxsize from
    settings (``tenant_budget_cache_*``); the cache absorbs the
    per-request ``SUM(llm_usage)`` cost on cache miss (Task 2).
    """
    global _budget_snapshot_cache
    if _budget_snapshot_cache is None:
        settings = get_settings()
        _budget_snapshot_cache = TenantBudgetSnapshotCache(
            ttl_s=settings.tenant_budget_cache_ttl_s,
            maxsize=settings.tenant_budget_cache_maxsize,
            repo=TenantBudgetSnapshotRepository(),
        )
    return _budget_snapshot_cache


def _build_precheck_cache() -> PrecheckCache:
    """Construct the per-process ``PrecheckCache`` singleton (Pack B follow-up).

    TTL driven by ``tenant_budget_precheck_cache_ttl_s`` (default 5s).
    Hot tenants with no new ``llm_usage`` rows between turns pay zero
    DB round-trips for the pre-check; ``_post_record`` invalidates the
    entry on every successful write.
    """
    global _precheck_cache
    if _precheck_cache is None:
        settings = get_settings()
        _precheck_cache = PrecheckCache(
            ttl_seconds=settings.tenant_budget_precheck_cache_ttl_s,
        )
    return _precheck_cache


async def _default_llm_client_factory(tenant_id: str) -> LLMClient:
    """Build a per-tenant ``LLMClient`` via ``TenantResolver`` (+ ``BudgetResolver`` if configured).

    M4.C strict mode: tenants with no enabled provider configs raise
    :class:`TenantLlmNotConfigured` immediately at factory time.

    M4.D opt-in: tenants with a ``tenant_budgets`` row get their
    resolver wrapped with :class:`BudgetResolver`; no row = no
    enforcement (preserves existing M4.C behavior).

    Args:
        tenant_id: opaque tenant identifier; must exist in ``tenants``.

    Raises:
        TenantLlmNotConfigured: tenant has zero enabled provider configs.
    """
    tenant_cache = _build_tenant_cache()
    try:
        inner_resolver = await build_tenant_resolver(tenant_id, cache=tenant_cache)
    except TenantLlmNotConfigured:
        # Strict-mode rejection — surface to ops via the zero-label
        # counter. Spec §8.5 deliberately omits ``tenant_id`` from the
        # label set so cardinality stays bounded and PII never leaks
        # via metric scraping. The structured log line below carries
        # the tenant_id for ops triage.
        LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL.inc()
        raise

    # M4.D opt-in: wrap with BudgetResolver only when a budget row exists.
    budget = await TenantBudgetRepository().get_by_tenant(tenant_id)
    resolver = inner_resolver
    if budget is not None:
        resolver = BudgetResolver(
            inner=inner_resolver,
            tenant_id=tenant_id,
            budget=budget,
            snapshot_cache=_build_budget_snapshot_cache(),
            precheck_cache=_build_precheck_cache(),
        )

    return LLMClient(
        provider_resolver=resolver,
        tenant_id=tenant_id,
        usage_recorder=UsageRecorder(),
    )


def _resolve_default_model() -> str:
    """Resolve the effective default model from settings.

    (Unchanged from M4.B; preserved for backward compatibility with
    :class:`SimpleResponder.__init__`.)
    """
    from agent.simple_responder import DEFAULT_MODEL

    settings = get_settings()
    if settings.minimax_api_key:
        return settings.minimax_model or "MiniMax-M3"
    return DEFAULT_MODEL


def _build_http_pool() -> HttpClientPool:
    """Lazily build the process-wide :class:`HttpClientPool` singleton.

    Mirrors the lazy-init pattern used by :func:`_build_tenant_cache` /
    :func:`_build_budget_snapshot_cache`: nothing happens until the
    first LLM call, so a process that never talks to a provider
    (e.g. an admin-only Arq worker) doesn't pay the construction cost.
    """
    global _http_pool
    if _http_pool is None:
        _http_pool = HttpClientPool()
    return _http_pool


async def aclose_http_pool() -> None:
    """Close every cached client in the pool and drop the singleton.

    Idempotent. Called from the FastAPI lifespan on shutdown so the
    underlying TCP sockets are released; safe to call when the pool
    was never built (no-op).

    Symmetric with :func:`llm_client.embeddings.aclose_default_client`.
    """
    global _http_pool
    if _http_pool is not None:
        await _http_pool.aclose_all()
        _http_pool = None


def reset_http_pool() -> None:
    """Test helper: drop the pool reference without closing clients.

    Pairs with the conftest's ``_reset_singletons`` autouse fixture so
    each pytest-asyncio test (each with its own event loop) starts
    with a fresh pool. The previous pool's clients are abandoned to
    the garbage collector — the loop that owned them is already
    closing, so ``aclose()`` would be unsafe.
    """
    global _http_pool
    _http_pool = None


__all__ = [
    "_build_budget_snapshot_cache",
    "_build_http_pool",
    "_build_precheck_cache",
    "_build_tenant_cache",
    "_default_llm_client_factory",
    "_resolve_default_model",
    "aclose_http_pool",
    "reset_http_pool",
]