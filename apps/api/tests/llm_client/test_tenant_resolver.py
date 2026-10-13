"""Tests for TenantLLMConfigCache + TenantResolverSpan wrapper."""
from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_client.tenant_resolver import TenantLLMConfigCache
from llm_client.tenant_resolver_span import TenantResolverSpan


class _FakeRepo:
    def __init__(self, rows_by_tenant: dict[str, list[Any]]) -> None:
        self.rows_by_tenant = rows_by_tenant
        self.call_count = 0

    async def list_by_tenant(self, tenant_id: str, *, enabled_only: bool = True) -> list[Any]:
        self.call_count += 1
        return list(self.rows_by_tenant.get(tenant_id, []))


def _make_provider(name: str) -> Any:
    """Return a stub BaseProvider carrying the .name attribute LLMClient expects."""
    p = MagicMock()
    p.name = name
    p.chat = AsyncMock()
    p.stream = AsyncMock()
    p.aclose = AsyncMock()
    return p


def test_cache_hit_skips_db_lookup() -> None:
    repo = _FakeRepo({"t1": [MagicMock()]})
    cache = TenantLLMConfigCache(ttl_s=60.0, maxsize=16)
    cache._build_providers = MagicMock(return_value={"minimax": _make_provider("minimax")})  # type: ignore[attr-defined]
    cache.put("t1", "fake-resolver")
    assert cache.get("t1") == "fake-resolver"
    assert repo.call_count == 0


def test_cache_miss_loads_from_repo() -> None:
    repo = _FakeRepo({"t1": [MagicMock()]})
    cache = TenantLLMConfigCache(ttl_s=60.0, maxsize=16)
    cache._build_providers = MagicMock(return_value={"minimax": _make_provider("minimax")})  # type: ignore[attr-defined]
    cache._repo = repo  # type: ignore[attr-defined]
    cache.get_or_load("t1")
    cache.get_or_load("t1")  # second call hits cache
    assert repo.call_count == 1


def test_cache_respects_ttl() -> None:
    repo = _FakeRepo({"t1": [MagicMock()]})
    cache = TenantLLMConfigCache(ttl_s=0.01, maxsize=16)
    cache._build_providers = MagicMock(return_value={"minimax": _make_provider("minimax")})  # type: ignore[attr-defined]
    cache._repo = repo  # type: ignore[attr-defined]
    cache.get_or_load("t1")
    time.sleep(0.05)  # past TTL
    cache.get_or_load("t1")
    assert repo.call_count == 2


def test_cache_lru_evicts_oldest() -> None:
    repo = _FakeRepo({f"t{i}": [MagicMock()] for i in range(5)})
    cache = TenantLLMConfigCache(ttl_s=60.0, maxsize=3)
    cache._build_providers = MagicMock(return_value={"minimax": _make_provider("minimax")})  # type: ignore[attr-defined]
    cache._repo = repo  # type: ignore[attr-defined]
    cache.get_or_load("t0")
    cache.get_or_load("t1")
    cache.get_or_load("t2")
    cache.get_or_load("t3")  # t0 evicted
    assert cache.get("t0") is None  # evicted (oldest)
    assert cache.get("t1") is not None  # still cached
    assert cache.get("t2") is not None  # still cached
    assert cache.get("t3") is not None  # just inserted


def test_raises_when_no_enabled_providers() -> None:
    from llm_client.exceptions import TenantLlmNotConfigured

    repo = _FakeRepo({"t1": []})
    cache = TenantLLMConfigCache(ttl_s=60.0, maxsize=16)
    cache._build_providers = MagicMock(return_value={})  # type: ignore[attr-defined]
    cache._repo = repo  # type: ignore[attr-defined]
    with pytest.raises(TenantLlmNotConfigured) as exc:
        cache.get_or_load("t1")
    assert exc.value.tenant_id == "t1"


def test_missing_providers_listed_in_exception() -> None:
    """When providers is empty, the exception lists all known providers as missing.

    The test exercises ``_raise_or_return`` directly to assert the
    ``missing_providers`` field is computed against the ``known_providers``
    argument (which defaults to the M4.A registry name list).
    """
    from llm_client.exceptions import TenantLlmNotConfigured

    cache = TenantLLMConfigCache(ttl_s=60.0, maxsize=16)
    providers: dict[str, Any] = {}  # tenant has zero enabled configs
    with pytest.raises(TenantLlmNotConfigured) as exc:
        cache._raise_or_return(  # type: ignore[attr-defined]
            tenant_id="t1",
            providers=providers,
            known_providers=["minimax", "anthropic", "openai"],
        )
    # Every known provider should be reported missing when providers is empty.
    assert "minimax" in exc.value.missing_providers
    assert "anthropic" in exc.value.missing_providers
    assert "openai" in exc.value.missing_providers


def test_tenant_resolver_call_delegates_to_inner_gateway() -> None:
    primary = _make_provider("minimax")
    gateway = MagicMock()
    gateway.default_resolver = MagicMock(return_value=primary)
    resolver = TenantResolverSpan(inner=gateway.default_resolver, tenant_id="t1")
    result = resolver(MagicMock())
    assert result is primary


def test_single_provider_tenant_ainvoke_falls_back_to_sync() -> None:
    """Single-provider tenants whose inner resolver has no ``ainvoke``
    (e.g. ``_PrefixResolver``) fall back to sync ``__call__`` per the
    TenantResolverSpan docstring — the caller still gets a result.
    M6.8: replaces the legacy ``TenantResolver.ainvoke`` which raised
    ``_NoChainConfigured`` in this case.
    """
    primary = _make_provider("minimax")

    class _SyncOnlyResolver:
        """Stand-in for ``_PrefixResolver`` — sync only, no ``ainvoke``."""

        def __call__(self, request: Any) -> Any:  # noqa: ARG002
            return primary

    sync_resolver = _SyncOnlyResolver()
    resolver = TenantResolverSpan(inner=sync_resolver, tenant_id="t1")
    import asyncio
    result = asyncio.run(resolver.ainvoke(MagicMock()))
    assert result is primary


def test_chain_tenant_ainvoke_delegates() -> None:
    class _AsyncFallback:
        """Stand-in for ``FallbackResolver`` — has ``ainvoke``."""

        def __call__(self, request: Any) -> Any:  # noqa: ARG002
            raise NotImplementedError

        async def ainvoke(self, request: Any) -> str:  # noqa: ARG002
            return "response"

    resolver = TenantResolverSpan(inner=_AsyncFallback(), tenant_id="t1")
    import asyncio
    result = asyncio.run(resolver.ainvoke(MagicMock()))
    assert result == "response"


def test_build_providers_threads_anthropic_base_url_from_row(monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: E501
    """When a tenant_llm_configs row has ``base_url`` set, the resolver
    must thread it into AnthropicProvider (mirrors the OpenAI/MiniMax
    path which already honours ``row.base_url``). Regression: the
    original wiring ignored ``row.base_url`` for anthropic, which meant
    Claude Code's MiniMax Anthropic proxy couldn't be selected per
    tenant without code changes.
    """
    from llm_client.tenant_config_crypto import TenantLLMConfigCipher
    from llm_client.tenant_resolver import TenantLLMConfigCache

    cipher = TenantLLMConfigCipher(
        "nLJ6W1cj0isqEsiRqrg98WJ9EPVB7e-BM1ub1TIRO2Y="  # dev-only; matches docker-compose
    )
    cache = TenantLLMConfigCache(
        ttl_s=60.0,
        maxsize=16,
        cipher=cipher,
        settings=MagicMock(
            default_llm_model="MiniMax-M3",
            minimax_model=None,
            minimax_base_url=None,
            openai_model=None,
        ),
    )

    captured: dict[str, Any] = {}

    def fake_anthropic(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _make_provider("anthropic")

    monkeypatch.setattr(
        "llm_client.providers.anthropic_provider.AnthropicProvider", fake_anthropic
    )

    row = MagicMock()
    row.provider_name = "anthropic"
    row.encrypted_api_key = cipher.encrypt("sk-tenant-key")
    row.base_url = "https://api.minimaxi.com/anthropic"

    out = cache._build_providers([row])
    assert "anthropic" in out
    assert captured["api_key"] == "sk-tenant-key"
    assert captured["base_url"] == "https://api.minimaxi.com/anthropic"
    assert captured["model"] == "MiniMax-M3"


def test_build_providers_anthropic_omits_base_url_when_row_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the row's ``base_url`` is null we must NOT pass ``base_url=``
    into AnthropicProvider — the provider's default kicks in
    (api.anthropic.com). Mirrors provider_registry.py which uses
    conditional kwargs to keep the real-Anthropic callers unchanged.
    """
    from llm_client.tenant_config_crypto import TenantLLMConfigCipher
    from llm_client.tenant_resolver import TenantLLMConfigCache

    cipher = TenantLLMConfigCipher(
        "nLJ6W1cj0isqEsiRqrg98WJ9EPVB7e-BM1ub1TIRO2Y="  # dev-only; matches docker-compose
    )
    cache = TenantLLMConfigCache(
        ttl_s=60.0,
        maxsize=16,
        cipher=cipher,
        settings=MagicMock(
            default_llm_model="claude-3-5-sonnet-20241022",
            minimax_model=None,
            minimax_base_url=None,
            openai_model=None,
        ),
    )

    captured: dict[str, Any] = {}

    def fake_anthropic(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _make_provider("anthropic")

    monkeypatch.setattr(
        "llm_client.providers.anthropic_provider.AnthropicProvider", fake_anthropic
    )

    row = MagicMock()
    row.provider_name = "anthropic"
    row.encrypted_api_key = cipher.encrypt("sk-real-anthropic")
    row.base_url = None

    cache._build_providers([row])
    assert "base_url" not in captured


def test_build_providers_openai_uses_default_llm_model_not_openai_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the openai branch must read ``default_llm_model``,
    not ``openai_model``.

    2026-10-13: the live demo tenant pointed at the Doubao Ark endpoint
    surfaced ``AttributeError: 'Settings' object has no attribute
    'openai_model'`` in ``TenantLLMConfigCache._build_providers`` when
    constructing the OpenAI provider. ``Settings`` exposes
    ``default_llm_model`` (a single project-wide LLM default) but no
    ``openai_model`` field — the previous code tried to read
    ``self._settings.openai_model``, which Pydantic rejects as a missing
    attribute. The mock-based tests passed because ``MagicMock().x``
    returns another Mock (truthy) instead of raising, so the bug was
    invisible in CI. This test uses a real ``Settings`` instance to pin
    the contract.
    """
    from llm_client.tenant_config_crypto import TenantLLMConfigCipher
    from llm_client.tenant_resolver import TenantLLMConfigCache

    cipher = TenantLLMConfigCipher(
        "nLJ6W1cj0isqEsiRqrg98WJ9EPVB7e-BM1ub1TIRO2Y="  # dev-only; matches docker-compose
    )
    from core.config import Settings

    settings = Settings(
        default_llm_model="claude-3-5-sonnet-20241022",
    )
    cache = TenantLLMConfigCache(
        ttl_s=60.0,
        maxsize=16,
        cipher=cipher,
        settings=settings,
    )

    captured: dict[str, Any] = {}

    def fake_openai(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _make_provider("openai")

    monkeypatch.setattr(
        "llm_client.providers.openai_provider.OpenAIProvider", fake_openai
    )

    row = MagicMock()
    row.provider_name = "openai"
    row.encrypted_api_key = cipher.encrypt("sk-tenant-openai-key")
    row.base_url = "https://ark.cn-beijing.volces.com/api/plan/v3"

    cache._build_providers([row])

    # model must come from default_llm_model (the only project-wide
    # model setting) and the row's base_url must be threaded through
    # so OpenAI-compatible servers (Doubao Ark, etc.) get routed.
    assert captured["model"] == "claude-3-5-sonnet-20241022"
    assert captured["base_url"] == "https://ark.cn-beijing.volces.com/api/plan/v3"