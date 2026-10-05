"""Tests for _default_llm_client_factory BYOK wiring (M4.C Task 3).

These tests verify the factory:

1. Builds an ``LLMClient`` whose ``provider_resolver`` is the result of
   ``await build_tenant_resolver(tenant_id, cache=cache)`` — the
   tenant-scoped resolver path replaces the M4.A/B project-wide
   registry build.
2. Raises :class:`TenantLlmNotConfigured` when the tenant has zero
   enabled configs (strict mode — no silent fallback).

The factory's cache is a module-level singleton. We patch
``_build_tenant_cache`` so each test gets a fresh fake cache and
nothing leaks between tests. The factory itself is a plain
``async def`` — the test infrastructure runs async tests under
``pytest-asyncio``'s ``auto`` mode (see ``pyproject.toml``).

Note: ``build_tenant_resolver`` calls ``cache.get(tenant_id)`` for
cache hits, not ``get_or_load`` — so we patch ``cache.get`` /
``cache._repo.list_by_tenant`` to drive the cache-miss and
not-configured paths.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.llm_factory import _default_llm_client_factory
from llm_client.exceptions import TenantLlmNotConfigured


async def test_factory_uses_tenant_resolver() -> None:
    """Factory awaits ``build_tenant_resolver`` and wires the result.

    The factory's ``provider_resolver`` MUST be exactly the resolver
    the cache returned (not a freshly-built one — that would defeat
    the cache). ``tenant_id`` round-trips into ``LLMClient.tenant_id``
    so usage attribution keeps working.
    """
    fake_resolver = MagicMock(name="cached-resolver")
    fake_cache = MagicMock()
    # ``build_tenant_resolver`` consults ``cache.get(tenant_id)`` first;
    # returning a real resolver here simulates a cache hit.
    fake_cache.get.return_value = fake_resolver

    with patch("agent.llm_factory._build_tenant_cache", return_value=fake_cache):
        client = await _default_llm_client_factory(tenant_id="t1")

    assert client.tenant_id == "t1"
    assert client.provider_resolver is fake_resolver
    # And: cache.get was consulted with the tenant id (cache-hit path).
    fake_cache.get.assert_called_once_with("t1")


async def test_factory_raises_tenant_not_configured() -> None:
    """Strict mode: zero enabled configs → TenantLlmNotConfigured surfaces.

    Spec §8.4 forbids silently falling back to the project-wide keys
    when the tenant has no BYOK configs. The factory re-raises
    TenantLlmNotConfigured (which subclasses ProviderUnavailable so
    existing LLMClient error handling still works) so the API layer
    can map it to a 503-style response.

    The mock chain simulates a cache miss (cache.get returns None)
    followed by a DB lookup that returns zero enabled rows. The
    cache's ``_raise_or_return`` then raises the exception; the
    factory re-raises it unchanged.
    """
    fake_cache = MagicMock()
    fake_cache.get.return_value = None  # cache miss
    fake_repo = MagicMock()
    fake_repo.list_by_tenant = AsyncMock(return_value=[])
    fake_cache._repo = fake_repo
    fake_cache._build_providers = MagicMock(return_value={})
    # ``_raise_or_return`` MUST raise TenantLlmNotConfigured when the
    # decrypted provider dict is empty — that's the strict-mode
    # boundary per spec §8.4.
    fake_cache._raise_or_return = MagicMock(
        side_effect=TenantLlmNotConfigured(
            tenant_id="t1", missing_providers=["anthropic"]
        )
    )

    with patch("agent.llm_factory._build_tenant_cache", return_value=fake_cache):
        with pytest.raises(TenantLlmNotConfigured) as excinfo:
            await _default_llm_client_factory(tenant_id="t1")

    assert excinfo.value.tenant_id == "t1"
    assert "anthropic" in excinfo.value.missing_providers

