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
from budget.repository import (
    TenantBudgetRepository,
    TenantBudgetSnapshotRepository,
)
from budget.resolver import BudgetResolver
from core.business_metrics import LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL
from core.config import get_settings
from llm_client.client import LLMClient
from llm_client.exceptions import TenantLlmNotConfigured
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


__all__ = [
    "_build_budget_snapshot_cache",
    "_build_tenant_cache",
    "_default_llm_client_factory",
    "_resolve_default_model",
]