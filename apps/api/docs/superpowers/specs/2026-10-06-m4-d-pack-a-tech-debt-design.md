# M4.D Pack A — Tech Debt Follow-up (4 项)

> **Status:** Approved for implementation (brainstorming complete on 2026-10-06)
> **Predecessor:** M4.D shipped at commit `c318602` (origin/main)
> **Scope:** Closes tech-debt items #3 (TTL window), #4 (soft-warn stickiness), #7 (refresh race), #1 (auto cleanup)
> **Companion packs:** Pack B (mid-features #2, #5, #8), Pack C (non-LLM cost #6)

## 1. Goals

1. **#3 零不一致窗口** — `BudgetResolver._pre_check` reads from DB directly (not cache) so cap enforcement sees the latest `tokens_used`. The ≤60s overshoot window in M4.D §11 is closed.
3. **#4 Sticky soft-warn** — `tenant_budget_snapshots.soft_warn_fired_at` records when soft-warn fired; only fires once per period even if admin lowers the cap mid-period.
4. **#7 No refresh race** — `refresh()` acquires `pg_advisory_xact_lock` so concurrent SUM() runs against the same (tenant, period) serialize; different (tenant, period) do not block.
5. **#1 Auto cleanup** — Daily Arq task deletes snapshot rows older than 13 months (12 audit + 1 buffer); admin can trigger manually.

## 2. Non-Goals

- ❌ Top-up mechanism (deferred to Pack B, tech-debt #2)
- ❌ Per-model breakdown (deferred to Pack B, tech-debt #5)
- ❌ Provider 429 integration (deferred to Pack B, tech-debt #8)
- ❌ Non-LLM cost budget (deferred to Pack C, tech-debt #6)
- ❌ Changes to LLMClient / Resolver / ainvoke interfaces
- ❌ Backward-incompatible changes to admin API contract

## 3. Background

M4.D shipped a per-tenant monthly token hard-cap with 8 documented tech-debt items (see `README.md` §"M4.D known tech debt"). This spec addresses the 4 items grouped under "Pack A — operational hygiene + correctness": they all touch snapshot writes / refresh, share code paths, and can ship together with minimal risk.

Pack A does not address customer-facing UX improvements (Pack B) or cost-category expansion (Pack C). After Pack A, the M4.D §11 tech-debt list will reduce from 8 items to 4 items, all of which are deferred to Pack B/C.

## 4. Architecture

The 4 fixes compose within the existing M4.A/B/C/D resolver chain without changing external interfaces:

```
LLMClient.chat()
  ↓
BudgetResolver.ainvoke(request)
  ├─ _pre_check()  ───────────────  #3 reads DB directly (not cache)
  ├─ inner.ainvoke(request)
  └─ _post_record(tokens_consumed)  #3 eager UPSERT + #4 sticky soft-warn
       ├─ → fire soft-warn if soft_warn_fired_at IS NULL AND threshold cross
       └─ → set_tokens_used(tokens_used=new, soft_warn_fired_at=now)
       ↓
  snapshot_repo.set_tokens_used()  ─────────────  #3 atomic UPSERT (no TTL stale)


TenantBudgetSnapshotRepository.refresh()  ────────  #7 advisory lock
  BEGIN TRANSACTION
  SELECT pg_advisory_xact_lock(hashtext(tenant_id || ':' || period))
  SELECT COALESCE(SUM(prompt+completion), 0) FROM llm_usage WHERE ...
  INSERT/UPDATE tenant_budget_snapshots
  COMMIT  ← lock released


Arq worker  ───────────────────────────────────  #1 daily task
  budget_cleanup_task (02:00 UTC) → DELETE LIMIT 10000 WHERE period < cutoff
```

### 4.1 Composition invariants

- **M4.A seam preserved** — `LLMClient.__init__` / `Resolver` / `ainvoke` interfaces unchanged.
- **M4.C strict mode preserved** — `_pre_check` order: cap → TenantLlmNotConfigured.
- **Existing tests preserved** — 31 M4.D tests stay green; behavior changes are observable only via new tests.

## 5. Components

### 5.1 #3 Eager write + DB-direct pre-check

**Files:**
- Modify: `apps/api/src/budget/resolver.py`
- Modify: `apps/api/src/budget/repository.py`
- Modify: `apps/api/src/core/config.py`

**Changes:**

`BudgetResolver._pre_check` reads from DB (not cache):

```python
async def _pre_check(self) -> None:
    if self._budget is None or self._budget.hard_cap_tokens is None:
        return
    period, period_start = _current_period(self._budget.period_anchor_tz)
    # NEW: DB-direct read for pre-check (no cache)
    snap = await self._snapshot_repo.get_for_tenant_period(
        self._tenant_id, period
    )
    if snap is None:
        # Cache miss → refresh via SUM() (which holds advisory lock)
        snap = await self._snapshot_repo.refresh(
            tenant_id=self._tenant_id,
            period=period,
            period_starts_at=period_start,
        )
    if snap.tokens_used >= self._budget.hard_cap_tokens:
        LLM_TENANT_BUDGET_EXCEEDED_TOTAL.inc()
        raise TenantBudgetExceeded(...)
```

`TenantBudgetSnapshotRepository.get_for_tenant_period` already exists (from M4.D Task 2). No signature change.

**New setting:**

```python
# core/config.py — add after M4.D's tenant_budget_cache_* fields
tenant_budget_pre_check_use_db: bool = Field(
    default=True,
    alias="TENANT_BUDGET_PRE_CHECK_USE_DB",
    description="When True, BudgetResolver._pre_check reads from DB (not cache) to eliminate the ≤60s overshoot window. Default True. Set False only for backward-compatibility with tenants requiring ≤1-call overshoot tolerance.",
)
```

When `False`, behavior falls back to M4.D's cache-based `_pre_check`. This is a soft escape hatch; default is strict.

**Side effect on the cache layer:** After Pack A, the `TenantBudgetSnapshotCache` is read from only when `tenant_budget_pre_check_use_db=False`, and is invalidated on every `_post_record`. In the default (strict) configuration, the cache becomes dead code. Removing `TenantBudgetSnapshotCache` entirely is a future tech-debt follow-up — out of scope for Pack A.

### 5.2 #4 Sticky soft-warn

**Files:**
- Create: `apps/api/migrations/versions/18_add_soft_warn_fired_at.py`
- Modify: `apps/api/src/budget/models.py`
- Modify: `apps/api/src/budget/repository.py`
- Modify: `apps/api/src/budget/resolver.py`

**Migration:**

```python
# 18_add_soft_warn_fired_at.py
"""add soft_warn_fired_at to tenant_budget_snapshots

Revision ID: 18_add_soft_warn_fired_at
Revises: 17_add_tenant_budgets
"""
from alembic import op
import sqlalchemy as sa

revision = "18_add_soft_warn_fired_at"
down_revision = "17_add_tenant_budgets"


def upgrade() -> None:
    op.add_column(
        "tenant_budget_snapshots",
        sa.Column(
            "soft_warn_fired_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # No backfill needed — existing rows have soft_warn_fired_at=NULL,
    # which is interpreted as "not yet fired in this period".


def downgrade() -> None:
    op.drop_column("tenant_budget_snapshots", "soft_warn_fired_at")
```

**Model:**

```python
# apps/api/src/budget/models.py — TenantBudgetSnapshot
class TenantBudgetSnapshot(Base):
    __tablename__ = "tenant_budget_snapshots"
    __table_args__ = (UniqueConstraint("tenant_id", "period", name="uq_tenant_budget_snapshots_tenant_period"),)

    # ... existing fields ...
    soft_warn_fired_at: datetime | None = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
```

**Repository:**

```python
# apps/api/src/budget/repository.py — TenantBudgetSnapshotRepository.set_tokens_used
async def set_tokens_used(
    self,
    *,
    tenant_id: str,
    period: str,
    tokens_used: int,
    soft_warn_fired_at: datetime | None = None,  # NEW
) -> TenantBudgetSnapshot:
    """UPSERT tokens_used + optional soft_warn_fired_at."""
    sm = get_sessionmaker()
    async with sm() as session:
        set_clause = {"tokens_used": tokens_used}
        if soft_warn_fired_at is not None:
            set_clause["soft_warn_fired_at"] = soft_warn_fired_at
        stmt = (
            pg_insert(TenantBudgetSnapshot)
            .values(
                id=new_guid(),
                tenant_id=tenant_id,
                period=period,
                tokens_used=tokens_used,
                soft_warn_fired_at=soft_warn_fired_at,
            )
            .on_conflict_do_update(
                constraint="uq_tenant_budget_snapshots_tenant_period",
                set_=set_clause,
            )
            .returning(TenantBudgetSnapshot)
        )
        ...
```

**Resolver:**

```python
# apps/api/src/budget/resolver.py — BudgetResolver._post_record
async def _post_record(self, tokens_consumed: int) -> None:
    period, period_start = _current_period(
        self._budget.period_anchor_tz if self._budget else "UTC"
    )
    snap = await self._snapshot_repo.get_for_tenant_period(
        self._tenant_id, period
    ) or await self._snapshot_repo.refresh(
        tenant_id=self._tenant_id,
        period=period,
        period_starts_at=period_start,
    )
    new_used = snap.tokens_used + tokens_consumed

    # Sticky soft-warn: fire ONCE per period
    fire_soft_warn = (
        self._budget is not None
        and self._budget.soft_warn_tokens is not None
        and snap.soft_warn_fired_at is None  # NEW: not yet fired this period
        and snap.tokens_used < self._budget.soft_warn_tokens <= new_used
    )

    soft_warn_fired_at = snap.soft_warn_fired_at  # carry over existing value
    if fire_soft_warn:
        LLM_TENANT_BUDGET_SOFT_WARN_TOTAL.inc()
        soft_warn_fired_at = datetime.now(timezone.utc)
        logger.warning(
            "tenant_budget.soft_warn",
            extra={
                "tenant_id": self._tenant_id,
                "period": period,
                "tokens_used": new_used,
                "soft_warn_tokens": self._budget.soft_warn_tokens,
                "hard_cap_tokens": self._budget.hard_cap_tokens,
            },
        )

    await self._snapshot_repo.set_tokens_used(
        tenant_id=self._tenant_id,
        period=period,
        tokens_used=new_used,
        soft_warn_fired_at=soft_warn_fired_at,  # NEW
    )
    self._snapshot_cache.invalidate(self._tenant_id, period=period)
```

**Behavior change:**

- **Before Pack A**: Soft-warn fires on EVERY call that crosses threshold (subject to ordering bugs from cache stale).
- **After Pack A**: Soft-warn fires once per `(tenant, period)`. Once `soft_warn_fired_at IS NOT NULL`, no more fires until period rollover or manual `UPDATE soft_warn_fired_at = NULL` via admin endpoint (deferred to Pack B).

### 5.3 #7 Advisory lock in refresh

**Files:**
- Modify: `apps/api/src/budget/repository.py`

**Changes:**

```python
# apps/api/src/budget/repository.py — TenantBudgetSnapshotRepository.refresh
async def refresh(
    self,
    *,
    tenant_id: str,
    period: str,
    period_starts_at: datetime,
) -> TenantBudgetSnapshot:
    """Run SUM(prompt+completion) and write a snapshot, holding an advisory lock
    so concurrent refreshes for the same (tenant, period) serialize.
    """
    from llm_client.models import LLMUsage
    from sqlalchemy import text

    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():  # explicit transaction so advisory_xact_lock auto-releases
            # Advisory lock keyed by hash(tenant_id || ':' || period)
            # → same (tenant, period) serializes, different combos do not block
            await session.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtext(:key))"
                ),
                {"key": f"{tenant_id}:{period}"},
            )
            sum_expr = func.coalesce(
                func.sum(LLMUsage.prompt_tokens + LLMUsage.completion_tokens), 0
            )
            result = await session.execute(
                select(sum_expr).where(
                    LLMUsage.tenant_id == tenant_id,
                    LLMUsage.created_at >= period_starts_at.replace(tzinfo=None),
                )
            )
            total = int(result.scalar_one())
            stmt = (
                pg_insert(TenantBudgetSnapshot)
                .values(
                    id=new_guid(),
                    tenant_id=tenant_id,
                    period=period,
                    tokens_used=total,
                )
                .on_conflict_do_update(
                    constraint="uq_tenant_budget_snapshots_tenant_period",
                    set_={
                        "tokens_used": total,
                        "last_refreshed_at": func.now(),
                    },
                )
                .returning(TenantBudgetSnapshot)
            )
            ins_result = await session.execute(stmt)
            row_obj = ins_result.scalar_one()
            await session.refresh(row_obj)
            return row_obj
```

**Behavior change:**

- **Before Pack A**: Two concurrent `refresh()` calls for the same `(tenant, period)` both run SUM() → both write to the same snapshot row (last-writer-wins via UPSERT). Not data-corrupting but wasted CPU.
- **After Pack A**: First `refresh()` holds advisory lock; second waits up to `lock_timeout` (PG default 0 = wait indefinitely). When first commits, second's SUM() runs against the freshest llm_usage (includes rows added since first's read). Correct.

### 5.4 #1 Auto cleanup arq task

**Files:**
- Create: `apps/api/src/budget/cleanup.py`
- Modify: `apps/api/src/core/config.py`
- Modify: `apps/api/src/workers/<existing>.py` (locate via grep for `qa_judge_worker` to find the worker registration module) — register `budget_cleanup_task` (cron `0 2 * * *`)
- Modify: `apps/api/src/admin/api.py` — add `POST /admin/budget/cleanup` endpoint
- Modify: `apps/api/src/admin/schemas/budget.py` — `CleanupResponse` schema
- Modify: `apps/api/src/core/business_metrics.py` — `LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL` zero-label counter

**Cleanup function:**

```python
# apps/api/src/budget/cleanup.py
"""Daily cleanup task for tenant_budget_snapshots.

Deletes rows older than ``tenant_budget_cleanup_retention_months`` (default 13)
in LIMIT 10000 batches to avoid long-running transactions.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func

from budget.models import TenantBudgetSnapshot
from core.business_metrics import LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL
from core.config import get_settings
from core.database import get_sessionmaker

logger = logging.getLogger(__name__)


async def run_budget_cleanup() -> dict[str, Any]:
    """Delete snapshot rows older than retention cutoff. Returns stats dict.

    Designed to be idempotent and safe to retry — DELETE only targets
    rows strictly older than the cutoff, so re-running is a no-op.
    """
    settings = get_settings()
    retention_months = settings.tenant_budget_cleanup_retention_months
    cutoff = (
        datetime.now(timezone.utc).replace(day=1) - relativedelta(months=retention_months)
    ).strftime("%Y-%m")

    sm = get_sessionmaker()
    total_deleted = 0
    while True:
        async with sm() as session:
            async with session.begin():
                stmt = (
                    delete(TenantBudgetSnapshot)
                    .where(TenantBudgetSnapshot.period < cutoff)
                    .execution_options(synchronize_session=False)
                    .limit(10000)
                )
                result = await session.execute(stmt)
                deleted = result.rowcount or 0
                total_deleted += deleted
                if deleted < 10000:
                    break

    LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL.inc(total_deleted)
    logger.info(
        "budget_cleanup.completed",
        extra={"deleted_rows": total_deleted, "cutoff_period": cutoff},
    )
    return {"deleted_rows": total_deleted, "cutoff_period": cutoff}


__all__ = ["run_budget_cleanup"]
```

**New setting:**

```python
# core/config.py — append after M4.D fields
tenant_budget_cleanup_retention_months: int = Field(
    default=13,
    alias="TENANT_BUDGET_CLEANUP_RETENTION_MONTHS",
    description="Cleanup task deletes tenant_budget_snapshots rows older than this many months. Default 13 = 12 audit + 1 buffer.",
)
```

**Arq registration:**

The project already uses Arq (see `apps/api/src/workers/` for qa_judge_worker pattern). Add `budget_cleanup_task` next to it. Schedule: cron `0 2 * * *` (02:00 UTC daily).

**Admin endpoint:**

```python
# apps/api/src/admin/api.py
@router.post(
    "/budget/cleanup",
    response_model=CleanupResponse,
)
async def trigger_budget_cleanup(
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> CleanupResponse:
    """Manually trigger budget snapshot cleanup.

    Cross-tenant: only callable by super-admin (no tenant_id in claims).
    Returns stats from the cleanup run.
    """
    # Anti-enumeration: any non-super-admin token gets 404
    if claims.get("tenant_id") is not None:
        raise HTTPException(status_code=404, detail="not found")
    stats = await run_budget_cleanup()
    return CleanupResponse(deleted_rows=stats["deleted_rows"], cutoff_period=stats["cutoff_period"])
```

**Counter:**

```python
# core/business_metrics.py — append after M4.D counters
LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL = Counter(
    "lumen_budget_cleanup_rows_deleted_total",
    "Number of tenant_budget_snapshots rows deleted by the cleanup task.",
)
```

## 6. Data Flow Walkthrough

**Scenario:** Tenant with budget (`hard_cap=10000`, `soft_warn=8000`, `period_anchor_tz="Asia/Shanghai"`). Current period: `2026-10`. Period start: `2026-10-01T00:00+08:00`. Snapshot: `tokens_used=7900, soft_warn_fired_at=NULL`.

**Chat call consuming 500 tokens:**

```
LLMClient.chat(request)
  → BudgetResolver.ainvoke(request)
    → _pre_check()  [PACK A #3: DB-direct]
      ├─ period, period_start = _current_period("Asia/Shanghai")
      │     → period="2026-10", period_start=2026-10-01T00:00+08:00
      ├─ snap = await snapshot_repo.get_for_tenant_period("t1", "2026-10")
      │     → {tokens_used: 7900, soft_warn_fired_at: NULL, ...}
      ├─ if snap.tokens_used (7900) >= hard_cap (10000)? NO
      └─ return
    → resp = await inner.ainvoke(request)  (consumed 500 tokens)
    → _post_record(500)  [PACK A #3+#4]
      ├─ snap = get_for_tenant_period (or refresh if absent)
      ├─ new_used = 7900 + 500 = 8400
      ├─ STICKY CHECK [PACK A #4]:
      │     snap.soft_warn_fired_at IS NULL  ✓
      │     AND snap.tokens_used (7900) < soft_warn (8000) <= new_used (8400)  ✓
      │     → fire soft-warn + set soft_warn_fired_at = now()
      └─ set_tokens_used(tenant_id="t1", period="2026-10",
                         tokens_used=8400, soft_warn_fired_at=now())
```

**Same period, next call consuming 200 tokens:**

```
_post_record(200)
  ├─ snap.tokens_used = 8400, snap.soft_warn_fired_at IS NOT NULL
  ├─ new_used = 8400 + 200 = 8600
  ├─ STICKY CHECK: snap.soft_warn_fired_at IS NOT NULL → FALSE
  │     → NO fire (sticky)
  └─ set_tokens_used(tokens_used=8600, soft_warn_fired_at=snap.soft_warn_fired_at)
       (carry-over preserves the original fire timestamp)
```

**Next day 02:00 UTC — Arq cleanup:**

```
budget_cleanup_task
  → run_budget_cleanup()
    ├─ retention_months = 13
    ├─ cutoff = "2025-09"
    ├─ DELETE WHERE period < '2025-09' LIMIT 10000
    └─ return {deleted_rows: N, cutoff_period: "2025-09"}
```

**Cache miss triggers refresh with advisory lock:**

```
refresh("t1", "2026-10", "2026-10-01T00:00+08:00")
  BEGIN TRANSACTION
  SELECT pg_advisory_xact_lock(hashtext('t1:2026-10'))  [PACK A #7]
  SELECT COALESCE(SUM(...), 0) FROM llm_usage WHERE ...
  INSERT/UPDATE tenant_budget_snapshots
  COMMIT  ← advisory lock auto-released
```

## 7. Error Handling

| Scenario | Behavior |
|---|---|
| `refresh()` advisory lock contention | PG blocks (no timeout by default); second refresh waits for first's release |
| `set_tokens_used` write fails | SQLAlchemy exception propagates; `BudgetResolver._post_record` re-raises; LLMClient returns 5xx; tokens not recorded (conservative) |
| `soft_warn_fired_at` UPDATE fails | Same transaction as `tokens_used` — both fail atomically; no "fired but token not recorded" split |
| Arq cleanup DELETE fails | Arq auto-retries entire task; idempotent (re-running on same cutoff is no-op) |
| Arq cleanup long transaction | `LIMIT 10000` per batch keeps each transaction under ~1s even on 1M-row tables; arq worker `max_tries=3` + idempotency handles mid-batch retry |
| Cache invalidate fails | Exception swallowed; cache remains stale but next refresh will fetch fresh value |
| Migration `18_*` fails | alembic error; migrations chain terminates; deploy rollback per project convention |

## 8. Cardinality Discipline

Inherited from M4.D:

- All counters remain **zero-label** (no `tenant_id` in labels).
- `LLM_TENANT_BUDGET_EXCEEDED_TOTAL`, `LLM_TENANT_BUDGET_SOFT_WARN_TOTAL`: unchanged.
- New `LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL`: zero-label.
- `tenant_id` appears only in structured log lines.

## 9. Testing

### 9.1 Unit tests

**`tests/budget/test_resolver.py` (3 new):**
- `test_post_record_eager_write_eliminates_ttl_window` — back-to-back calls: call 1 sets tokens_used=7900; call 2's _pre_check reads from DB (not cache) and sees 7900; cap not exceeded; no over-spend.
- `test_pre_check_db_direct_when_setting_enabled` — explicit test that `tenant_budget_pre_check_use_db=True` (default) routes via DB.
- `test_pre_check_cache_fallback_when_setting_disabled` — with setting disabled, falls back to cache (legacy behavior).

**`tests/budget/test_resolver.py` (3 new for sticky soft-warn):**
- `test_soft_warn_fires_once_per_period` — first call crossing threshold sets `soft_warn_fired_at`; second call crossing same threshold does NOT fire.
- `test_soft_warn_does_not_fire_when_already_fired` — pre-populate `soft_warn_fired_at = now()`; cross threshold; no metric increment.
- `test_soft_warn_fired_at_carries_over_in_set_tokens_used` — verify that subsequent `set_tokens_used` calls preserve the original `soft_warn_fired_at` (don't overwrite with NULL).

**`tests/budget/test_repository.py` (2 new):**
- `test_set_tokens_used_accepts_soft_warn_fired_at` — call with explicit timestamp; verify written.
- `test_refresh_acquires_advisory_lock` — verify `pg_advisory_xact_lock` is called (use mock or PG introspection).

**`tests/budget/test_cleanup.py` (4 new):**
- `test_cleanup_keeps_recent_periods` — insert snapshots for `2026-09`, `2026-08`; run cleanup with retention=13; both preserved.
- `test_cleanup_deletes_old_periods` — insert `2024-01`, `2024-02`; run cleanup; both deleted.
- `test_cleanup_batches_at_10000` — insert 25000 rows older than cutoff; verify cleanup loop runs ≥3 batches.
- `test_cleanup_idempotent` — run cleanup twice; second run returns `deleted_rows=0`.

### 9.2 Integration tests

**`tests/budget/integration/test_pack_a_e2e.py` (3 new via pytest-httpx):**
- `test_e2e_pack_a_no_overshoot_under_concurrent_calls` — simulate 10 concurrent chats with cap=1000; total consumed ≤ 1100 (allowing for in-flight race); pre-check rejects ≥ call 9.
- `test_e2e_soft_warn_fires_once_under_load` — 5 calls all crossing soft_warn threshold; verify metric incremented exactly once.
- `test_e2e_cleanup_admin_endpoint_requires_jwt` — without token → 401; with non-admin token → 403; with admin token (no tenant_id in claims) → 200 + stats.

### 9.3 Migration test

**`tests/budget/test_migration_18.py` (2 new):**
- `test_upgrade_adds_soft_warn_fired_at_column` — alembic upgrade head; verify column exists with type `TIMESTAMP WITH TIME ZONE`.
- `test_downgrade_drops_soft_warn_fired_at_column` — alembic downgrade -1; verify column gone.

### 9.4 Existing regression

All 31 M4.D tests must remain green. Specifically:

- `tests/budget/test_resolver.py` existing 9 tests (M4.D Task 3 + TZ fix + single-provider fix) stay green
- `tests/budget/test_repository.py` existing 5 tests stay green
- `tests/budget/test_cache.py` existing 4 tests stay green
- `tests/budget/test_models_smoke.py` 2 tests stay green
- `tests/agent/test_llm_factory_budget.py` 2 tests stay green
- `tests/admin/test_budget_api.py` 7 tests stay green
- `tests/budget/integration/test_budget_e2e.py` 3 tests stay green

## 10. Rollout

1. Merge spec PR (this document).
2. Run the writing-plans skill to produce an implementation plan from this spec.
3. Execute the plan via subagent-driven-development (foundation → repository → resolver → admin cleanup → close-out).
4. Run full test suite; verify all 31 existing M4.D tests stay green and all 18 new Pack A tests pass.
4. Apply migration `18_*` to staging; verify column adds correctly.
5. Deploy to staging; monitor for 24h (verify cleanup task runs, soft-warn fires once, refresh locks serialize).
6. Deploy to production.
7. Update README to remove 4 closed tech-debt items; add 4 remaining items (#2, #5, #6, #8) as Pack B/C follow-up.

## 11. Tech Debt Remaining After Pack A

4 items remain:

1. **#2 No top-up mechanism** — deferred to Pack B
2. **#5 No per-model breakdown** — deferred to Pack B
3. **#6 No non-LLM cost budget** — deferred to Pack C
4. **#8 No provider 429 integration** — deferred to Pack B

## 12. File Map

| File | Status | Responsibility |
|---|---|---|
| `apps/api/migrations/versions/18_add_soft_warn_fired_at.py` | New | Migration: add `soft_warn_fired_at` column to `tenant_budget_snapshots` |
| `apps/api/src/budget/models.py` | Modify | Add `soft_warn_fired_at` to `TenantBudgetSnapshot` |
| `apps/api/src/budget/repository.py` | Modify | `set_tokens_used` accepts optional `soft_warn_fired_at`; `refresh()` acquires advisory lock |
| `apps/api/src/budget/resolver.py` | Modify | `_pre_check` reads from DB (not cache) when `tenant_budget_pre_check_use_db=True`; `_post_record` sticky soft-warn check + update |
| `apps/api/src/budget/cleanup.py` | New | `run_budget_cleanup()` function with batched DELETE |
| `apps/api/src/core/config.py` | Modify | Add 2 fields: `tenant_budget_pre_check_use_db` + `tenant_budget_cleanup_retention_months` |
| `apps/api/src/core/business_metrics.py` | Modify | Add `LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL` |
| `apps/api/src/admin/schemas/budget.py` | Modify | Add `CleanupResponse` schema |
| `apps/api/src/admin/api.py` | Modify | Add `POST /admin/budget/cleanup` endpoint |
| `apps/api/src/workers/<existing>.py` | Modify | Register `budget_cleanup_task` (cron `0 2 * * *`) |
| `apps/api/tests/budget/test_resolver.py` | Modify | +6 new tests (#3, #4) |
| `apps/api/tests/budget/test_repository.py` | Modify | +2 new tests (#7) |
| `apps/api/tests/budget/test_cleanup.py` | New | +4 tests (#1 cleanup function) |
| `apps/api/tests/budget/integration/test_pack_a_e2e.py` | New | +3 e2e tests |
| `apps/api/tests/admin/test_budget_api.py` | Modify | +1 new test (cleanup endpoint auth) |
| `apps/api/tests/budget/test_migration_18.py` | New | +2 tests (upgrade + downgrade) |
| `README.md` | Modify | Remove 4 closed tech-debt items; remaining 4 items get Pack B/C annotations |
| `~/.claude/projects/.../memory/m4-d-pack-a-progress.md` | New | Memory file |
| `~/.claude/projects/.../memory/MEMORY.md` | Modify | Add Pack A pointer |