"""Default LLMClient factory — builds tenant-scoped LLMClient instances.

Stage 7+ will make this per-tenant configurable (different providers,
different model names, per-tenant API keys, etc.).
"""
from __future__ import annotations

from llm_client.client import LLMClient
from llm_client.providers.anthropic_provider import AnthropicProvider
from llm_client.providers.openai_provider import OpenAIProvider
from llm_client.usage import UsageRecorder


def _default_llm_client_factory(tenant_id: str) -> LLMClient:
    """Build a per-tenant LLMClient wrapping the configured LLM provider.

    Provider selection (first match wins):

    1. **MiniMax** — when ``MINIMAX_API_KEY`` is set, an OpenAI-compatible
       ``OpenAIProvider`` pointed at ``MINIMAX_BASE_URL`` with model
       ``MINIMAX_MODEL`` (supports SSE streaming via ``stream()``).
    2. **Anthropic** — the M1 default, from ``ANTHROPIC_API_KEY`` +
       ``DEFAULT_LLM_MODEL``.

    For M1 there is no per-tenant API key — every tenant shares the
    project-wide key from settings. The ``tenant_id`` is still threaded
    through so usage recording is attributed to the tenant and per-tenant
    overrides can be layered on later without changing call sites.
    """
    from core.config import get_settings

    settings = get_settings()

    if settings.minimax_api_key:
        provider = OpenAIProvider(
            api_key=settings.minimax_api_key,
            model=settings.minimax_model or "MiniMax-M3",
            base_url=settings.minimax_base_url or "https://api.minimaxi.com/v1",
        )
        return LLMClient(
            default_provider=provider,
            tenant_id=tenant_id,
            usage_recorder=UsageRecorder(),
        )

    if not settings.anthropic_api_key:
        raise RuntimeError(
            "No LLM provider configured: set MINIMAX_API_KEY or "
            "ANTHROPIC_API_KEY in the environment before starting the API."
        )
    anthropic_provider = AnthropicProvider(
        api_key=settings.anthropic_api_key,
        model=settings.default_llm_model,
    )
    return LLMClient(
        default_provider=anthropic_provider,
        tenant_id=tenant_id,
        usage_recorder=UsageRecorder(),
    )


def _resolve_default_model() -> str:
    """Resolve the effective default model from settings.

    Returns ``MINIMAX_MODEL`` when MiniMax is configured, otherwise
    ``DEFAULT_LLM_MODEL``. Called at runtime so a config change takes
    effect without restarting the agent.
    """
    from agent.simple_responder import DEFAULT_MODEL
    from core.config import get_settings

    settings = get_settings()
    if settings.minimax_api_key:
        return settings.minimax_model or "MiniMax-M3"
    return DEFAULT_MODEL

