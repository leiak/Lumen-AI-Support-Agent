"""Default LLMClient factory — builds tenant-scoped LLMClient instances
backed by a per-tenant LLMGateway via BYOK config (M4.C).

Each tenant's provider keys are read from ``tenant_llm_configs``
(Fernet-encrypted, see :class:`TenantLLMConfigCipher`), decrypted, and
cached in-process. The cache absorbs the per-request DB + decrypt cost
after the first call. Tenants with zero enabled providers raise
:class:`TenantLlmNotConfigured` — strict mode, no silent fallback to
the project-wide keys.

See M4.C spec §7 (data flow) and §8.4 (strict-mode boundary).
"""
from __future__ import annotations

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


# Module-level singleton cache (per process). Tests reset this via
# ``agent.llm_factory._tenant_cache = None`` when they need a fresh
# cipher (e.g. after monkeypatching ``TENANT_LLM_FERNET_KEY``).
_tenant_cache: TenantLLMConfigCache | None = None


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


async def _default_llm_client_factory(tenant_id: str) -> LLMClient:
    """Build a per-tenant ``LLMClient`` via the :class:`TenantResolver`.

    Strict mode: tenants with no enabled configs raise
    :class:`TenantLlmNotConfigured` immediately at factory time — the
    caller (API endpoint) catches and returns a 503-style response.

    Args:
        tenant_id: opaque tenant identifier; must exist in ``tenants``
            table.

    Raises:
        TenantLlmNotConfigured: tenant has zero enabled provider configs.
    """
    cache = _build_tenant_cache()
    try:
        resolver = await build_tenant_resolver(tenant_id, cache=cache)
    except TenantLlmNotConfigured:
        # Strict-mode rejection — surface to ops via the zero-label
        # counter. Spec §8.5 deliberately omits ``tenant_id`` from the
        # label set so cardinality stays bounded and PII never leaks
        # via metric scraping. The structured log line below carries
        # the tenant_id for ops triage.
        LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL.inc()
        raise
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
    "_build_tenant_cache",
    "_default_llm_client_factory",
    "_resolve_default_model",
]
