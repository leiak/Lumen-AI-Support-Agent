"""Tests for factory wiring of BudgetResolver (M4.D Task 3).

Mirrors M4.C's test_llm_factory_tenant.py pattern: each test gets
fresh fake caches / repos via ``unittest.mock.patch`` so nothing
leaks between tests.

What we verify:
1. No ``tenant_budgets`` row → factory's resolver is the inner one
   (NOT wrapped with ``BudgetResolver``). M4.D is opt-in per tenant.
2. ``tenant_budgets`` row present → factory wraps the inner resolver
   with ``BudgetResolver``, attaching the row snapshot.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from agent.llm_factory import _default_llm_client_factory
from budget.resolver import BudgetResolver
from llm_client.client import LLMClient


async def test_factory_skips_budget_wrap_when_no_row() -> None:
    """No tenant_budgets row → inner resolver is NOT wrapped with BudgetResolver.

    Strict-mode (``TenantLlmNotConfigured``) is unaffected; budget
    enforcement is opt-in per tenant — existing M4.C tenants see no
    behavior change.
    """
    fake_resolver = MagicMock(name="inner-resolver")
    fake_cache = MagicMock()
    # ``build_tenant_resolver`` consults ``cache.get(tenant_id)`` first;
    # returning a real resolver here simulates a cache hit.
    fake_cache.get.return_value = fake_resolver

    # No budget row for this tenant
    fake_budget_repo = MagicMock()
    fake_budget_repo.get_by_tenant = AsyncMock(return_value=None)

    with patch(
        "agent.llm_factory._build_tenant_cache", return_value=fake_cache
    ), patch(
        "agent.llm_factory.TenantBudgetRepository",
        return_value=fake_budget_repo,
    ):
        client = await _default_llm_client_factory(tenant_id="t1")

    # client is LLMClient, provider_resolver is the inner resolver unchanged
    assert isinstance(client, LLMClient)
    assert client.provider_resolver is fake_resolver
    assert not isinstance(client.provider_resolver, BudgetResolver)
    # And: budget lookup was attempted (opt-in check ran)
    fake_budget_repo.get_by_tenant.assert_awaited_once_with("t1")


async def test_factory_wraps_with_budget_resolver_when_row_exists() -> None:
    """tenant_budgets row present → BudgetResolver wraps the inner resolver.

    The wrapped resolver's ``_inner`` is the cached tenant resolver,
    ``_tenant_id`` is the tenant_id, ``_budget`` is the row from
    ``TenantBudgetRepository.get_by_tenant``, and ``_snapshot_cache``
    is the singleton from ``_build_budget_snapshot_cache``.
    """
    from budget.models import TenantBudget

    fake_inner = MagicMock(name="inner-resolver")
    fake_tenant_cache = MagicMock()
    fake_tenant_cache.get.return_value = fake_inner

    # Budget row with all three cap fields set
    fake_budget = MagicMock(spec=TenantBudget)
    fake_budget.soft_warn_tokens = 800
    fake_budget.hard_cap_tokens = 1000
    fake_budget.period_anchor_tz = "UTC"

    fake_budget_repo = MagicMock()
    fake_budget_repo.get_by_tenant = AsyncMock(return_value=fake_budget)
    fake_snapshot_cache = MagicMock(name="snapshot-cache")

    with patch(
        "agent.llm_factory._build_tenant_cache", return_value=fake_tenant_cache
    ), patch(
        "agent.llm_factory._build_budget_snapshot_cache",
        return_value=fake_snapshot_cache,
    ), patch(
        "agent.llm_factory.TenantBudgetRepository",
        return_value=fake_budget_repo,
    ):
        client = await _default_llm_client_factory(tenant_id="t1")

    # Wrapped with BudgetResolver, inner is the cached resolver
    assert isinstance(client.provider_resolver, BudgetResolver)
    assert client.provider_resolver._inner is fake_inner
    assert client.provider_resolver._tenant_id == "t1"
    assert client.provider_resolver._budget is fake_budget
    assert client.provider_resolver._snapshot_cache is fake_snapshot_cache