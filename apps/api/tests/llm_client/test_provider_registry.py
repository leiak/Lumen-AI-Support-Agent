"""Unit tests for llm_client.provider_registry.build_provider_registry."""
from unittest.mock import patch

import pytest

from llm_client.provider_registry import build_provider_registry


def _settings(**overrides):  # type: ignore[no-untyped-def]
    """Build a Settings instance from overrides without reading .env.

    The factory only reads the fields listed below — we construct a
    Settings directly. pydantic-settings will validate the field set;
    required fields without overrides will raise, but our factory reads
    only the keys we override.
    """
    from core.config import Settings

    # ``SettingsConfigDict`` does not set ``populate_by_name=True``, so
    # pydantic-settings only accepts the field aliases (uppercase env
    # names) as kwargs. Passing the lower-cased field names silently
    # falls through to .env / shell values.
    defaults = {
        "MINIMAX_API_KEY": None,
        "MINIMAX_BASE_URL": None,
        "MINIMAX_MODEL": None,
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_BASE_URL": None,
        "OPENAI_API_KEY": None,
        "DEFAULT_LLM_MODEL": "claude-3-5-sonnet-20241022",
        "DATABASE_URL": "postgresql+asyncpg://x:y@localhost:5432/z",
        "REDIS_URL": "redis://localhost:6379/0",
        "JWT_SECRET": "test-secret-32-chars-minimum-length",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[call-arg]


def test_registry_includes_minimax_when_minimax_key_set() -> None:
    settings = _settings(MINIMAX_API_KEY="k")
    with patch("llm_client.provider_registry.OpenAIProvider") as mock_openai:
        reg = build_provider_registry(settings)
    assert "minimax" in reg
    mock_openai.assert_called_once()
    call_kwargs = mock_openai.call_args.kwargs
    assert call_kwargs["api_key"] == "k"
    assert call_kwargs["model"] == "MiniMax-M3"  # default fallback
    assert "api.minimaxi.com" in call_kwargs["base_url"]


def test_registry_includes_anthropic_when_anthropic_key_set() -> None:
    # Explicit ANTHROPIC_BASE_URL=None to shadow the shell value Claude
    # Code exports (the host's env typically carries the MiniMax proxy
    # URL, which would otherwise sneak into Settings via pydantic-settings).
    settings = _settings(ANTHROPIC_API_KEY="anth-k", ANTHROPIC_BASE_URL=None)
    with patch("llm_client.provider_registry.AnthropicProvider") as mock_anth:
        reg = build_provider_registry(settings)
    assert "anthropic" in reg
    mock_anth.assert_called_once_with(
        api_key="anth-k", model="claude-3-5-sonnet-20241022"
    )


def test_registry_passes_anthropic_base_url_when_set() -> None:
    """When ANTHROPIC_BASE_URL is set the registry must thread it into
    AnthropicProvider so Claude Code's MiniMax proxy works without
    code changes. Mirrors the OpenAIProvider path which already accepts
    base_url (see test_registry_includes_minimax_when_minimax_key_set).
    """
    settings = _settings(
        ANTHROPIC_API_KEY="anth-k",
        ANTHROPIC_BASE_URL="https://api.minimaxi.com/anthropic",
    )
    with patch("llm_client.provider_registry.AnthropicProvider") as mock_anth:
        build_provider_registry(settings)
    mock_anth.assert_called_once_with(
        api_key="anth-k",
        model="claude-3-5-sonnet-20241022",
        base_url="https://api.minimaxi.com/anthropic",
    )


def test_registry_includes_both_when_both_keys_set() -> None:
    settings = _settings(MINIMAX_API_KEY="k", ANTHROPIC_API_KEY="a")
    with patch("llm_client.provider_registry.OpenAIProvider"), \
         patch("llm_client.provider_registry.AnthropicProvider"):
        reg = build_provider_registry(settings)
    assert set(reg.keys()) == {"minimax", "anthropic"}


def test_registry_uses_minimax_model_override_when_set() -> None:
    settings = _settings(MINIMAX_API_KEY="k", MINIMAX_MODEL="custom-m")
    with patch("llm_client.provider_registry.OpenAIProvider") as mock_openai:
        build_provider_registry(settings)
    assert mock_openai.call_args.kwargs["model"] == "custom-m"


def test_registry_raises_when_no_keys_set() -> None:
    settings = _settings()
    with pytest.raises(RuntimeError, match="No LLM provider configured"):
        build_provider_registry(settings)