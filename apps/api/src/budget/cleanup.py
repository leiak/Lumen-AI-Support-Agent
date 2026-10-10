"""Daily cleanup task for tenant_budget_snapshots (M4.D Pack A #1).

Deletes snapshot rows whose ``period`` (YYYY-MM) is strictly less than
the cutoff = N months before the current period. Default retention
13 months = 12 audit + 1 buffer. Operates in LIMIT 10000 batches to
avoid long-running transactions.

Idempotent: DELETE only targets rows strictly older than the cutoff,
so re-running is a no-op. Arq retries the whole task on transient
failure; the batched design means a mid-batch failure loses at most
1 batch's worth of work.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select

from budget.models import TenantBudgetSnapshot
from core.business_metrics import LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL
from core.config import get_settings
from core.database import get_sessionmaker

logger = logging.getLogger(__name__)

_CLEANUP_BATCH_SIZE = 10000


def _cutoff_period(months_back: int) -> str:
    """Return the YYYY-MM cutoff period for ``months_back`` months ago.

    Plain integer arithmetic on (year, month) — no dateutil / relativedelta
    (dateutil is not a project dependency). Handles year boundaries
    correctly: months_back=14 in 2026-01 → (2024, 11).
    """
    now = datetime.now(UTC)
    y, m = now.year, now.month - months_back
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


async def run_budget_cleanup() -> dict[str, Any]:
    """Delete snapshot rows older than the retention cutoff.

    Returns:
        Dict with ``deleted_rows`` (int) and ``cutoff_period`` (str).

    Designed to be called from the arq ``budget_cleanup_task`` cron
    (registered at 02:00 UTC daily) AND the admin
    ``POST /admin/budget/cleanup`` endpoint (manual trigger).
    """
    settings = get_settings()
    retention_months = settings.tenant_budget_cleanup_retention_months
    cutoff = _cutoff_period(retention_months)

    sm = get_sessionmaker()
    total_deleted = 0
    while True:
        async with sm() as session:
            async with session.begin():
                # PostgreSQL does NOT support DELETE ... LIMIT natively;
                # use a subquery to bound the row set. Translate to:
                #   DELETE FROM tenant_budget_snapshots
                #   WHERE id IN (
                #     SELECT id FROM tenant_budget_snapshots
                #     WHERE period < :cutoff LIMIT :batch_size
                #   )
                stmt = (
                    delete(TenantBudgetSnapshot)
                    .where(
                        TenantBudgetSnapshot.id.in_(
                            select(TenantBudgetSnapshot.id)
                            .where(TenantBudgetSnapshot.period < cutoff)
                            .limit(_CLEANUP_BATCH_SIZE)
                        )
                    )
                    .execution_options(synchronize_session=False)
                )
                result = await session.execute(stmt)
                deleted = int(result.rowcount or 0)
                total_deleted += deleted
                if deleted < _CLEANUP_BATCH_SIZE:
                    break

    if total_deleted > 0:
        LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL.inc(total_deleted)
    logger.info(
        "budget_cleanup.completed",
        extra={"deleted_rows": total_deleted, "cutoff_period": cutoff},
    )
    return {"deleted_rows": total_deleted, "cutoff_period": cutoff}


__all__ = ["run_budget_cleanup"]