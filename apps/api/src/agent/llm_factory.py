"""Default LLMClient factory — builds tenant-scoped LLMClient instances.

Stage 7+ will make this per-tenant configurable (different providers,
different model names, per-tenant API keys, etc.).
"""
from __future__ import annotations

from llm_client.client import LLMClient
from llm_client.providers.anthropic_provider import AnthropicProvider
from llm_client.usage import UsageRecorder


def _default_llm_client_factory(tenant_id: str) -> LLMClient:
    """Build a per-tenant LLMClient wrapping AnthropicProvider.

    For M1 there is no per-tenant API key — every tenant shares the
    project-wide Anthropic key from settings. The ``tenant_id`` is still
    threaded through so usage recording is attributed to the tenant and
    per-tenant overrides can be layered on later without changing call
    sites.
    """
    from core.config import get_settings

    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not configured; set it in the environment "
            "before starting the API."
        )
    provider = AnthropicProvider(
        api_key=settings.anthropic_api_key,
        model=settings.default_llm_model,
    )
    return LLMClient(
        default_provider=provider,
        tenant_id=tenant_id,
        usage_recorder=UsageRecorder(),
    )