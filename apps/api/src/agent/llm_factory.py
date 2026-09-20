"""Default LLMClient factory — builds tenant-scoped LLMClient instances
backed by a project-wide LLMGateway.

The gateway holds the provider registry (built from settings via
:func:`llm_client.provider_registry.build_provider_registry`) and
exposes a default prefix-based resolver. ``_default_llm_client_factory``
wraps that resolver in a fresh ``LLMClient`` per tenant so usage
recording is attributed to the correct tenant.

Per-tenant provider/model overrides are deferred to M4.C (BYOK). For
M4.A every tenant shares the project-wide gateway; the ``tenant_id``
is still threaded through so the existing usage path keeps working.

Gateway lifecycle is per-request (not per-process). The gateway
itself is lightweight (it only holds a dict of provider references);
the underlying ``httpx.AsyncClient`` lives on each provider instance
and is closed via ``LLMGateway.aclose_all`` on API shutdown. Per-call
construction is acceptable for M4.A — future M4.C work can introduce
caching in a lifespan-aware singleton without changing this call
site's signature.
"""
from __future__ import annotations

from llm_client.client import LLMClient
from llm_client.gateway import LLMGateway
from llm_client.provider_registry import build_provider_registry
from llm_client.usage import UsageRecorder


def _default_llm_client_factory(tenant_id: str) -> LLMClient:
    """Build a per-tenant ``LLMClient`` wrapping the gateway's default resolver.

    Args:
        tenant_id: opaque tenant identifier threaded into ``LLMClient`` so
            usage rows are attributed correctly. Every tenant currently
            shares the same provider registry; per-tenant overrides live
            in M4.C.

    Raises:
        RuntimeError: if no LLM provider is configured (re-raised from
            ``build_provider_registry``). The error message contains the
            env var names an operator must set — safe to surface; no PII.
    """
    from core.config import get_settings

    settings = get_settings()
    gateway = LLMGateway(providers=build_provider_registry(settings))
    return LLMClient(
        provider_resolver=gateway.default_resolver,
        tenant_id=tenant_id,
        usage_recorder=UsageRecorder(),
    )


def _resolve_default_model() -> str:
    """Resolve the effective default model from settings.

    Returns ``MINIMAX_MODEL`` (or ``"MiniMax-M3"`` as a fallback) when
    MiniMax is configured, otherwise the ``DEFAULT_MODEL`` constant from
    ``agent.simple_responder``. Called at runtime so a config change
    takes effect without restarting the agent.

    Why this duplicates the factory's "is minimax configured?" branch
    instead of reading ``gateway.providers[gateway.default_provider_name].model``:
    the two are NOT equivalent for the Anthropic-only path. The
    gateway hands ``AnthropicProvider`` ``settings.default_llm_model``
    (the configured Anthropic model), but this function returns
    ``DEFAULT_MODEL = "claude-haiku-4-5"`` for the same path — a
    deliberate cost optimisation for chat, untouched by M4.A. Calling
    site is just :class:`SimpleResponder.__init__`, so the duplicate
    branch stays small and explicit. Refactor if M4.A ever aligns the
    two values.
    """
    from agent.simple_responder import DEFAULT_MODEL
    from core.config import get_settings

    settings = get_settings()
    if settings.minimax_api_key:
        return settings.minimax_model or "MiniMax-M3"
    return DEFAULT_MODEL


__all__ = ["_default_llm_client_factory", "_resolve_default_model"]
