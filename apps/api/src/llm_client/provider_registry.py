"""Build the provider registry from :class:`core.config.Settings`.

Replaces the inline provider wiring that used to live in
:func:`agent.llm_factory._default_llm_client_factory`. Centralizing
here means the gateway is constructed identically from any call site
(production factory, QA Judge, history mining, tests).

YAGNI: only MiniMax + Anthropic are wired. OpenAI etc. can be added
later by following the same pattern. The ``_PrefixResolver`` already
recognizes ``gpt-`` / ``o1-`` / ``o3-`` prefixes, so dropping a
``providers["openai"]`` entry into the registry is enough to activate
OpenAI routing — no resolver changes needed.
"""
from __future__ import annotations

from core.config import Settings
from llm_client.providers.anthropic_provider import AnthropicProvider
from llm_client.providers.base import BaseProvider
from llm_client.providers.openai_provider import OpenAIProvider

# Default model for MiniMax when MINIMAX_MODEL isn't configured. Matches
# the M1 factory wiring (kept verbatim so behavior is unchanged for
# existing deployments).
_DEFAULT_MINIMAX_MODEL = "MiniMax-M3"
_DEFAULT_MINIMAX_BASE_URL = "https://api.minimaxi.com/v1"


def build_provider_registry(settings: Settings) -> dict[str, BaseProvider]:
    """Build the provider registry keyed by name.

    Insertion order matters: ``LLMGateway`` uses the first key as the
    default provider when no ``default_provider_name`` is supplied.
    MiniMax is registered first so it wins over Anthropic when both
    keys are configured — this matches the M1 factory behavior.
    """
    providers: dict[str, BaseProvider] = {}
    if settings.minimax_api_key:
        providers["minimax"] = OpenAIProvider(
            api_key=settings.minimax_api_key,
            model=settings.minimax_model or _DEFAULT_MINIMAX_MODEL,
            base_url=settings.minimax_base_url or _DEFAULT_MINIMAX_BASE_URL,
        )
    if settings.anthropic_api_key:
        providers["anthropic"] = AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=settings.default_llm_model,
        )
    if not providers:
        raise RuntimeError(
            "No LLM provider configured: set MINIMAX_API_KEY or "
            "ANTHROPIC_API_KEY before starting the API."
        )
    return providers


__all__ = ["build_provider_registry"]
