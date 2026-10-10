"""Pack B end-to-end: credit grant → effective cap → breakdown → gate.

Marks ``@pytest.mark.integration`` so the default selector
(``pytest -m "not integration"``) skips it. Requires a live Postgres
testcontainer — matches the convention from
``tests/budget/integration/test_pack_a_e2e.py``.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from budget.credits import CreditService
from budget.exceptions import TenantBudgetRateLimited
from budget.models import TenantBudget, TenantBudgetSnapshot
from budget.per_model import PerModelBreakdownCache, PerModelService
from budget.resolver import BudgetResolver
from core.business_metrics import LLM_BUDGET_GATE_TOTAL
from core.id_gen import new_id
from llm_client.exceptions import RateLimited
from llm_client.models import LLMUsage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_session() -> AsyncGenerator[object, None]:
    """Yield an ``AsyncSession`` from the project sessionmaker.

    Local copy of the same fixture pattern used by other integration
    conftests (``tests/qa/integration/conftest.py``,
    ``tests/admin/conftest.py``). Lets the test seed raw rows
    (``TenantBudget``, ``TenantBudgetSnapshot``, ``LLMUsage``) directly
    instead of going through repositories — needed to control the
    ``tokens_used=1450`` precondition that drives the gate below
    threshold.
    """
    from core.database import get_sessionmaker

    sm = get_sessionmaker()
    async with sm() as session:
        yield session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_e2e_credit_grant_then_breakdown_then_gate(
    db_session: object,
) -> None:
    """Full Pack B happy path + gate trigger.

    Steps:
        1. Super_admin grants 500 tokens → effective_cap becomes 1500.
        2. Per-model breakdown returns 2 entries summing to 1800.
        3. Resolver with snap at 1450 + 429 → gate fires
           (remaining=50 < 1000 default threshold).
        4. No post_record (no tokens billed because inner raised 429).
    """
    tenant_id = "tenant-e2e-packb"
    period = datetime.now(UTC).strftime("%Y-%m")

    budget = TenantBudget(
        id=new_id(),
        tenant_id=tenant_id,
        hard_cap_tokens=1000,
        soft_warn_tokens=2000,
        period_anchor_tz="UTC",
    )
    snap = TenantBudgetSnapshot(
        id=new_id(),
        tenant_id=tenant_id,
        period=period,
        tokens_used=0,
        last_refreshed_at=datetime.now(UTC),
    )
    db_session.add(budget)
    db_session.add(snap)

    # Seed two distinct (provider, model) llm_usage rows.
    for prov, model, p, c in [
        ("openai", "gpt-4o-mini", 700, 350),
        ("anthropic", "haiku", 500, 250),
    ]:
        db_session.add(
            LLMUsage(
                id=new_id(),
                tenant_id=tenant_id,
                provider=prov,
                model=model,
                prompt_tokens=p,
                completion_tokens=c,
                cost_usd=0.0,
                request_id="r",
                cached=False,
                metadata_json={},
                created_at=datetime.now(UTC),
            )
        )
    await db_session.flush()

    # Pre-check passes against tokens_used=0. Gate fires after we mutate
    # snap.tokens_used to 1450 below — the mock below returns the same
    # mutated object for both _pre_check and _maybe_raise_budget_gate.
    snap.tokens_used = 1450

    # 1. Super_admin grants 500 tokens → effective_cap becomes 1500.
    cache = PerModelBreakdownCache(ttl_seconds=30)
    credit_svc = CreditService(db_session, cache)
    await credit_svc.grant(
        tenant_id=tenant_id,
        tokens=500,
        note="e2e",
        granted_by="super",
    )

    # 2. Per-model breakdown returns 2 entries summing to 1800.
    pm_svc = PerModelService(db_session, cache)
    rows = await pm_svc.get_breakdown(tenant_id, period)
    assert len(rows) == 2
    # 700+350+500+250 = 1800
    assert sum(r.total_tokens for r in rows) == 1800

    # 3. Resolver with snap at 1450 + 429 → gate fires (remaining=50 < 1000).
    snap_repo = MagicMock()
    snap_repo.get_for_tenant_period = AsyncMock(return_value=snap)
    snap_repo.upsert = AsyncMock()
    snap_repo.set_tokens_used = AsyncMock()
    snap_cache = MagicMock()
    snap_cache.get_or_load_async = AsyncMock(return_value=snap)
    snap_cache.invalidate = MagicMock()
    before_metric = LLM_BUDGET_GATE_TOTAL._value.get()  # type: ignore[attr-defined]

    inner = MagicMock()
    inner.ainvoke = AsyncMock(side_effect=RateLimited("429"))

    resolver = BudgetResolver(
        inner=inner,
        tenant_id=tenant_id,
        budget=budget,
        snapshot_cache=snap_cache,
        snapshot_repo=snap_repo,
        credit_service=credit_svc,
        per_model_cache=cache,
    )
    with pytest.raises(TenantBudgetRateLimited) as exc:
        await resolver.ainvoke(MagicMock())
    # effective_cap = 1000 (base) + 500 (credits) = 1500
    # remaining = 1500 - 1450 = 50
    assert exc.value.remaining == 50
    assert LLM_BUDGET_GATE_TOTAL._value.get() == before_metric + 1

    # 4. No post_record (no tokens billed — RateLimited propagated through).
    snap_repo.set_tokens_used.assert_not_called()


__all__ = ["test_e2e_credit_grant_then_breakdown_then_gate"]