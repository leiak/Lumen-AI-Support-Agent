"""Unit tests for :mod:`agent.llm_factory`.

These tests cover the M4.C tenant-resolver wiring of
``_default_llm_client_factory``. The factory now:

1. Awaits ``build_tenant_resolver(tenant_id, cache=cache)`` to
   obtain a per-tenant resolver.
2. Wraps that resolver in a fresh ``LLMClient``.
3. Raises :class:`TenantLlmNotConfigured` (and increments
   ``lumen_llm_tenant_not_configured_total``) when the tenant
   has zero enabled provider configs.

We patch ``_build_tenant_cache`` at the factory module so the
real Fernet cipher + DB sessionmaker stack never spins up in
unit tests. The deeper behavior of the cache itself is
covered in :mod:`tests.llm_client.test_tenant_resolver`; this
file pins the **factory's** contract — argument shape,
resolver passthrough, error propagation, metric increment.

pytest-asyncio runs these under ``auto`` mode (see
``pyproject.toml``); each test is a plain ``async def``.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.llm_factory import _default_llm_client_factory
from core.business_metrics import LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL
from llm_client.client import LLMClient
from llm_client.exceptions import TenantLlmNotConfigured


def _fake_cache_with_resolver(resolver: object) -> MagicMock:
    """Return a MagicMock cache that hands out ``resolver`` on cache hit.

    ``build_tenant_resolver`` consults ``cache.get(tenant_id)`` first;
    returning the supplied resolver from ``cache.get`` simulates the
    cache-hit path so the factory skips the DB lookup entirely.
    """
    fake_cache = MagicMock()
    fake_cache.get.return_value = resolver
    return fake_cache


async def test_factory_returns_llm_client_with_cached_resolver() -> None:
    """Factory returns ``LLMClient`` wired to the cache's resolver.

    Pins the M4.A contract that callers depend on:
    ``factory(tenant_id).provider_resolver`` is the resolver the
    cache produced, and ``tenant_id`` round-trips onto the client
    so usage attribution keeps working.
    """
    fake_resolver = MagicMock(name="cached-resolver")
    fake_cache = _fake_cache_with_resolver(fake_resolver)

    with patch("agent.llm_factory._build_tenant_cache", return_value=fake_cache):
        client = await _default_llm_client_factory("t-test")

    assert isinstance(client, LLMClient)
    assert client.tenant_id == "t-test"
    assert client.provider_resolver is fake_resolver
    fake_cache.get.assert_called_once_with("t-test")


async def test_factory_preserves_tenant_id_for_distinct_calls() -> None:
    """``tenant_id`` round-trips per call — no module-global tenant leaks.

    Mirrors the M4.A contract: every call constructs a fresh
    ``LLMClient`` whose ``tenant_id`` matches the argument. The
    cache is per-tenant (keyed on tenant_id), but each LLMClient
    is freshly constructed so usage rows always attribute to the
    right tenant.
    """
    fake_resolver_a = MagicMock(name="resolver-a")
    fake_resolver_b = MagicMock(name="resolver-b")

    # First call
    with patch(
        "agent.llm_factory._build_tenant_cache",
        return_value=_fake_cache_with_resolver(fake_resolver_a),
    ):
        client_a = await _default_llm_client_factory("tenant-abc")
    # Second call, different tenant
    with patch(
        "agent.llm_factory._build_tenant_cache",
        return_value=_fake_cache_with_resolver(fake_resolver_b),
    ):
        client_b = await _default_llm_client_factory("tenant-xyz")

    assert client_a.tenant_id == "tenant-abc"
    assert client_b.tenant_id == "tenant-xyz"
    assert client_a.tenant_id != client_b.tenant_id
    assert client_a.provider_resolver is fake_resolver_a
    assert client_b.provider_resolver is fake_resolver_b


async def test_factory_raises_tenant_not_configured() -> None:
    """Strict mode: tenant with zero enabled configs raises.

    The factory MUST re-raise ``TenantLlmNotConfigured`` unchanged
    so the API layer can map it to a 503-style response. We don't
    catch / wrap — letting it propagate is the contract.
    """
    fake_cache = MagicMock()
    fake_cache.get.return_value = None  # cache miss
    fake_repo = MagicMock()
    fake_repo.list_by_tenant = AsyncMock(return_value=[])
    fake_cache._repo = fake_repo  # type: ignore[attr-defined]
    fake_cache._raise_or_return = MagicMock(  # type: ignore[attr-defined]
        side_effect=TenantLlmNotConfigured(
            tenant_id="t-empty", missing_providers=["minimax", "anthropic"]
        )
    )

    with patch("agent.llm_factory._build_tenant_cache", return_value=fake_cache):
        with pytest.raises(TenantLlmNotConfigured) as excinfo:
            await _default_llm_client_factory("t-empty")

    assert excinfo.value.tenant_id == "t-empty"
    assert "minimax" in excinfo.value.missing_providers


async def test_factory_increments_metric_on_not_configured() -> None:
    """Each ``TenantLlmNotConfigured`` rejection bumps the metric.

    Spec §8.5: ``lumen_llm_tenant_not_configured_total`` is the
    ops signal that a tenant's BYOK config has been wiped or never
    set. We sample the value before and after to ensure the counter
    moves — ``prometheus_client.Counter`` doesn't expose a
    ``delta`` accessor, so we diff the read instead.
    """
    before = LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL._value.get()  # type: ignore[attr-defined]

    fake_cache = MagicMock()
    fake_cache.get.return_value = None
    fake_repo = MagicMock()
    fake_repo.list_by_tenant = AsyncMock(return_value=[])
    fake_cache._repo = fake_repo  # type: ignore[attr-defined]
    fake_cache._raise_or_return = MagicMock(  # type: ignore[attr-defined]
        side_effect=TenantLlmNotConfigured(
            tenant_id="t-metric", missing_providers=["anthropic"]
        )
    )

    with patch("agent.llm_factory._build_tenant_cache", return_value=fake_cache):
        with pytest.raises(TenantLlmNotConfigured):
            await _default_llm_client_factory("t-metric")

    after = LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL._value.get()  # type: ignore[attr-defined]
    assert after == before + 1


async def test_factory_does_not_increment_metric_on_success() -> None:
    """Successful cache hit — the not-configured counter MUST NOT move.

    The metric is for rejected calls only; a successful tenant
    resolution should never touch it. Catches a regression where
    the ``inc()`` is hoisted out of the ``except`` block.
    """
    fake_resolver = MagicMock(name="resolver-ok")
    fake_cache = _fake_cache_with_resolver(fake_resolver)

    before = LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL._value.get()  # type: ignore[attr-defined]

    with patch("agent.llm_factory._build_tenant_cache", return_value=fake_cache):
        client = await _default_llm_client_factory("t-ok")

    after = LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL._value.get()  # type: ignore[attr-defined]
    assert after == before
    assert client.provider_resolver is fake_resolver
