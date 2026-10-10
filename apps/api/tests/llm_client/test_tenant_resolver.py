"""Tests for TenantLLMConfigCache + TenantResolver."""
from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_client.tenant_resolver import (
    TenantLLMConfigCache,
    TenantResolver,
    _NoChainConfigured,
)


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
    resolver = TenantResolver(gateway=gateway)
    result = resolver(MagicMock())
    assert result is primary


def test_single_provider_tenant_ainvoke_raises_no_chain() -> None:
    primary = _make_provider("minimax")
    # _PrefixResolver has no ainvoke; simulates single-provider tenant
    prefix_resolver = MagicMock(spec=["__call__"])
    prefix_resolver.__call__ = MagicMock(return_value=primary)
    gateway = MagicMock()
    gateway.default_resolver = prefix_resolver
    resolver = TenantResolver(gateway=gateway)
    import asyncio
    with pytest.raises(_NoChainConfigured):
        asyncio.run(resolver.ainvoke(MagicMock()))


def test_chain_tenant_ainvoke_delegates() -> None:
    _primary = _make_provider("minimax")
    fallback = MagicMock(spec=["__call__", "ainvoke"])
    fallback.ainvoke = AsyncMock(return_value="response")
    gateway = MagicMock()
    gateway.default_resolver = fallback
    resolver = TenantResolver(gateway=gateway)
    import asyncio
    result = asyncio.run(resolver.ainvoke(MagicMock()))
    assert result == "response"