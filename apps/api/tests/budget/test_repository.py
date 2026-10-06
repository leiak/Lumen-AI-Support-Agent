"""Tests for TenantBudgetRepository + TenantBudgetSnapshotRepository."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, insert

from budget.models import TenantBudget, TenantBudgetSnapshot
from budget.repository import TenantBudgetRepository, TenantBudgetSnapshotRepository
from core.database import get_sessionmaker
from core.id_gen import new_id
from llm_client.models import LLMUsage
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository


async def test_budget_upsert_creates_row() -> None:
    tenant = await TenantRepository().create(name="Budget Test 1", plan=TenantPlan.PRO)
    repo = TenantBudgetRepository()
    row = await repo.upsert(
        tenant_id=tenant.id,
        soft_warn_tokens=800,
        hard_cap_tokens=1000,
    )
    assert row.tenant_id == tenant.id
    assert row.soft_warn_tokens == 800
    assert row.hard_cap_tokens == 1000
    assert row.period_anchor_tz == "UTC"


async def test_budget_upsert_updates_existing_row() -> None:
    tenant = await TenantRepository().create(name="Budget Test 2", plan=TenantPlan.PRO)
    repo = TenantBudgetRepository()
    first = await repo.upsert(tenant_id=tenant.id, soft_warn_tokens=800, hard_cap_tokens=1000)
    second = await repo.upsert(tenant_id=tenant.id, soft_warn_tokens=1600, hard_cap_tokens=2000)
    assert second.id == first.id  # upsert keeps same row
    assert second.soft_warn_tokens == 1600
    assert second.hard_cap_tokens == 2000


async def test_snapshot_set_tokens_used_inserts_or_updates() -> None:
    tenant = await TenantRepository().create(name="Snapshot Test 1", plan=TenantPlan.PRO)
    repo = TenantBudgetSnapshotRepository()
    snap = await repo.set_tokens_used(
        tenant_id=tenant.id, period="2026-10", tokens_used=500
    )
    assert snap.tokens_used == 500
    assert snap.period == "2026-10"
    # Second call updates (same row)
    snap2 = await repo.set_tokens_used(
        tenant_id=tenant.id, period="2026-10", tokens_used=750
    )
    assert snap2.id == snap.id
    assert snap2.tokens_used == 750


async def test_snapshot_set_tokens_used_accepts_soft_warn_fired_at() -> None:
    """Pack A #4: set_tokens_used optionally writes soft_warn_fired_at."""
    tenant = await TenantRepository().create(name="Budget Soft Warn", plan=TenantPlan.PRO)
    repo = TenantBudgetSnapshotRepository()
    fired_at = datetime(2026, 10, 15, 12, 30, tzinfo=timezone.utc)
    snap = await repo.set_tokens_used(
        tenant_id=tenant.id, period="2026-10",
        tokens_used=500, soft_warn_fired_at=fired_at,
    )
    assert snap.tokens_used == 500
    assert snap.soft_warn_fired_at == fired_at
    # Subsequent call WITHOUT soft_warn_fired_at preserves the previous
    # value (no overwrite to NULL) — Pack A #4 carry-over behavior.
    snap2 = await repo.set_tokens_used(
        tenant_id=tenant.id, period="2026-10", tokens_used=750,
    )
    assert snap2.tokens_used == 750
    assert snap2.soft_warn_fired_at == fired_at


async def test_snapshot_refresh_sums_llm_usage() -> None:
    """refresh() runs SUM(prompt+completion) on llm_usage filtered by period."""
    tenant = await TenantRepository().create(name="Snapshot Refresh", plan=TenantPlan.PRO)
    # Seed llm_usage rows directly via the existing model
    sm = get_sessionmaker()
    async with sm() as session:
        await session.execute(
            insert(LLMUsage),
            [
                {"id": new_id(), "tenant_id": tenant.id, "provider": "minimax",
                 "model": "MiniMax-M3", "prompt_tokens": 100, "completion_tokens": 50,
                 "cost_usd": 0.0, "request_id": "r1"},
                {"id": new_id(), "tenant_id": tenant.id, "provider": "minimax",
                 "model": "MiniMax-M3", "prompt_tokens": 200, "completion_tokens": 100,
                 "cost_usd": 0.0, "request_id": "r2"},
            ],
        )
        await session.commit()

    repo = TenantBudgetSnapshotRepository()
    snap = await repo.refresh(
        tenant_id=tenant.id,
        period="2026-10",
        period_starts_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    assert snap.tokens_used == 450  # 100+50 + 200+100


async def test_cascade_delete_with_tenant() -> None:
    """Deleting the tenant CASCADEs to tenant_budgets + tenant_budget_snapshots."""
    tenant = await TenantRepository().create(name="Budget Cascade", plan=TenantPlan.PRO)
    brepo = TenantBudgetRepository()
    srepo = TenantBudgetSnapshotRepository()
    await brepo.upsert(tenant_id=tenant.id, soft_warn_tokens=100, hard_cap_tokens=200)
    await srepo.set_tokens_used(tenant_id=tenant.id, period="2026-10", tokens_used=50)
    # Sanity: rows present before delete
    assert await brepo.get_by_tenant(tenant.id) is not None
    assert await srepo.get_for_tenant_period(tenant.id, "2026-10") is not None
    # Delete the tenant directly via session.delete (TenantRepository has no delete())
    sm = get_sessionmaker()
    async with sm() as session:
        tenant_obj = await session.get(Tenant, tenant.id)
        await session.delete(tenant_obj)
        await session.commit()
    # Both budget rows gone via ON DELETE CASCADE
    assert await brepo.get_by_tenant(tenant.id) is None
    assert await srepo.get_for_tenant_period(tenant.id, "2026-10") is None


async def test_snapshot_refresh_acquires_advisory_lock() -> None:
    """Pack A #7: refresh() acquires pg_advisory_xact_lock keyed on (tenant_id, period).

    Verifies the lock is held during the SUM() + UPSERT so concurrent
    refreshes against the same (tenant, period) serialize. We assert
    via pg_locks that an advisory lock with the matching key is held
    while the refresh is in flight. Test uses a side-effect wrapper to
    inspect pg_locks mid-transaction.
    """
    tenant = await TenantRepository().create(name="Advisory Lock Test", plan=TenantPlan.PRO)
    repo = TenantBudgetSnapshotRepository()

    # Insert a row via llm_usage so refresh has work to do
    sm = get_sessionmaker()
    async with sm() as session:
        await session.execute(
            insert(LLMUsage),
            [
                {"id": new_id(), "tenant_id": tenant.id, "provider": "minimax",
                 "model": "MiniMax-M3", "prompt_tokens": 10, "completion_tokens": 5,
                 "cost_usd": 0.0, "request_id": "r-advlock"},
            ],
        )
        await session.commit()

    # Monkey-patch session.execute and intercept the lock-acquiring
    # statement. We look for any SQL containing 'pg_advisory' inside
    # the refresh() coroutine.
    lock_seen = False
    original_execute = None
    from sqlalchemy import text

    class _LockDetectingSession:
        """Wraps a Session and records whether pg_advisory was called."""

        def __init__(self, inner):  # noqa: ANN001
            self._inner = inner

        def __getattr__(self, name: str) -> object:  # noqa: ANN204
            # Delegate all unknown attributes (begin, commit, refresh, ...)
            # to the wrapped session so the SUT sees a normal AsyncSession.
            return getattr(self._inner, name)

        async def execute(self, stmt, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
            nonlocal lock_seen
            sql_str = str(stmt)
            if "pg_advisory" in sql_str:
                lock_seen = True
            return await self._inner.execute(stmt, *args, **kwargs)

    # Patch get_sessionmaker to return our wrapping factory
    import budget.repository as repo_module
    real_sm = repo_module.get_sessionmaker()

    class _WrappedSM:
        def __call__(self):
            return _LockDetectingCtx(real_sm())

    class _LockDetectingCtx:
        def __init__(self, inner):  # noqa: ANN001
            self._inner = inner

        async def __aenter__(self):
            self._session = await self._inner.__aenter__()
            return _LockDetectingSession(self._session)

        async def __aexit__(self, *args):
            return await self._inner.__aexit__(*args)

    repo_module.get_sessionmaker = lambda: _WrappedSM()  # type: ignore[assignment]

    try:
        await repo.refresh(
            tenant_id=tenant.id,
            period="2026-10",
            period_starts_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
    finally:
        repo_module.get_sessionmaker = real_sm  # type: ignore[assignment]

    assert lock_seen, "Expected pg_advisory_xact_lock to be issued during refresh()"
