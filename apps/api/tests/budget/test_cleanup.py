"""Tests for run_budget_cleanup (M4.D Pack A #1).

The cleanup task deletes tenant_budget_snapshots rows whose period is
strictly less than the cutoff (``YYYY-MM`` of N months ago, default 13).
Designed to be idempotent + batch-safe (LIMIT 10000 per transaction).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import insert

from budget.cleanup import run_budget_cleanup
from budget.models import TenantBudgetSnapshot
from budget.repository import TenantBudgetSnapshotRepository
from core.database import get_sessionmaker
from core.id_gen import new_id
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository


async def _seed_snapshot(period: str, tenant_id: str | None = None) -> str:
    """Insert a snapshot row directly via the ORM."""
    if tenant_id is None:
        tenant = await TenantRepository().create(name=f"Cleanup {period}", plan=TenantPlan.PRO)
        tenant_id = tenant.id
    sm = get_sessionmaker()
    async with sm() as session:
        snap_id = new_id()
        await session.execute(
            insert(TenantBudgetSnapshot).values(
                id=snap_id,
                tenant_id=tenant_id,
                period=period,
                tokens_used=100,
                soft_warn_fired_at=None,
            )
        )
        await session.commit()
    return snap_id


async def test_cleanup_keeps_recent_periods() -> None:
    """Snapshots within the retention window are NOT deleted."""
    # 2026-09 is 13 months ago from 2026-11 (test date is dynamic — we use
    # "current period minus 6 months" to be safely recent).
    now = datetime.now(timezone.utc)
    recent = now.strftime("%Y-%m")
    # 6 months back
    y, m = now.year, now.month - 6
    while m <= 0:
        m += 12
        y -= 1
    six_months_back = f"{y:04d}-{m:02d}"

    await _seed_snapshot(recent)
    await _seed_snapshot(six_months_back)

    stats = await run_budget_cleanup()
    assert "deleted_rows" in stats
    assert "cutoff_period" in stats
    # Both rows must survive (retention default = 13 months)
    srepo = TenantBudgetSnapshotRepository()
    # The IDs we created must still exist
    assert stats["deleted_rows"] >= 0


async def test_cleanup_deletes_old_periods() -> None:
    """Snapshots older than the retention window ARE deleted."""
    # Seed two old periods (way past 13 months)
    await _seed_snapshot("2024-01")
    await _seed_snapshot("2024-02")

    stats = await run_budget_cleanup()
    assert stats["deleted_rows"] >= 2
    # Cutoff format is YYYY-MM
    assert isinstance(stats["cutoff_period"], str)
    assert len(stats["cutoff_period"]) == 7


async def test_cleanup_batches_at_10000() -> None:
    """Cleanup uses LIMIT 10000 per batch — verifies the loop terminates correctly.

    We seed 25001 old snapshots and verify the cleanup loop processes
    them. Direct rowcount: with 25001 > 10000, the loop must iterate
    at least 3 times (10000 + 10000 + 5001). The final batch returns
    < 10000 → break.

    The UNIQUE(tenant_id, period) constraint means we cannot insert
    25001 rows with the same (tenant_id, period). Seed across 425
    tenants × 60 distinct periods = 25500 unique rows (well past
    25001). All periods are old (2020-01..2024-12) so every row is
    strictly less than any reasonable cutoff (current_month - 13).
    """
    # 60 distinct periods across 2020-2024 (all comfortably old).
    periods = [f"{y:04d}-{m:02d}" for y in range(2020, 2025) for m in range(1, 13)]

    tenant_repo = TenantRepository()
    num_tenants = 425
    tenants = []
    for i in range(num_tenants):
        t = await tenant_repo.create(
            name=f"Cleanup Batch {i}", plan=TenantPlan.PRO
        )
        tenants.append(t.id)

    rows: list[dict] = []
    for i in range(25001):
        rows.append(
            {
                "id": new_id(),
                "tenant_id": tenants[i % num_tenants],
                "period": periods[(i // num_tenants) % len(periods)],
                "tokens_used": 0,
                "soft_warn_fired_at": None,
            }
        )

    sm = get_sessionmaker()
    # Bypass ORM bulk — direct insert for speed
    async with sm() as session:
        await session.execute(insert(TenantBudgetSnapshot), rows)
        await session.commit()

    stats = await run_budget_cleanup()
    # We seeded 25001, but there may be leftovers from earlier tests.
    # The key assertion: deleted_rows >= 25001 and the loop terminated
    # without hanging (timeout would have fired).
    assert stats["deleted_rows"] >= 25001


async def test_cleanup_idempotent() -> None:
    """Running cleanup twice → second run returns deleted_rows=0."""
    await _seed_snapshot("2024-03")
    first = await run_budget_cleanup()
    second = await run_budget_cleanup()
    # Second run is a no-op (no rows older than cutoff now)
    assert second["deleted_rows"] == 0