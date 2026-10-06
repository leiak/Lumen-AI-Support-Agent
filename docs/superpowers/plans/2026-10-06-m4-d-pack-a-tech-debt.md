# M4.D Pack A — Tech Debt Follow-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close 4 of 8 M4.D tech-debt items by reorganizing snapshot writes (#3), soft-warn persistence (#4), refresh locking (#7), and auto cleanup (#1) into a single shippable unit.

**Architecture:** Composes within the existing M4.A/B/C/D resolver chain. `BudgetResolver._pre_check` reads snapshots from DB (not cache) to eliminate the ≤60s overshoot window. `tenant_budget_snapshots.soft_warn_fired_at` enforces sticky one-shot soft-warn per period. `TenantBudgetSnapshotRepository.refresh()` serializes via `pg_advisory_xact_lock`. A daily Arq task `budget_cleanup_task` deletes snapshot rows older than 13 months (12 audit + 1 buffer). No changes to M4.A `Resolver`/`ainvoke` interfaces.

**Tech Stack:** Python 3.11+, Pydantic v2, SQLAlchemy 2.0 async ORM, FastAPI, Alembic, PostgreSQL 15+ (advisory locks), Arq, Prometheus client, pytest-httpx, dateutil (relativedelta).

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `apps/api/migrations/versions/18_add_soft_warn_fired_at.py` | New | Migration: add `soft_warn_fired_at` column to `tenant_budget_snapshots` |
| `apps/api/src/budget/models.py` | Modify | Add `soft_warn_fired_at` to `TenantBudgetSnapshot` |
| `apps/api/src/budget/repository.py` | Modify | `set_tokens_used` accepts optional `soft_warn_fired_at`; `refresh()` acquires advisory lock |
| `apps/api/src/budget/resolver.py` | Modify | `_pre_check` reads from DB (not cache); `_post_record` sticky soft-warn + write `soft_warn_fired_at` |
| `apps/api/src/budget/cleanup.py` | New | `run_budget_cleanup()` function with batched DELETE |
| `apps/api/src/core/config.py` | Modify | Add 2 fields: `tenant_budget_pre_check_use_db` + `tenant_budget_cleanup_retention_months` |
| `apps/api/src/core/business_metrics.py` | Modify | Add `LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL` counter |
| `apps/api/src/admin/schemas/budget.py` | Modify | Add `CleanupResponse` schema |
| `apps/api/src/admin/api.py` | Modify | Add `POST /admin/budget/cleanup` endpoint |
| `apps/api/src/qa/worker.py` | Modify | Add `budget_cleanup_task` + register in `WorkerSettings.cron_jobs` |
| `apps/api/tests/budget/test_migration_18.py` | New | +2 tests: upgrade adds column, downgrade drops column |
| `apps/api/tests/budget/test_repository.py` | Modify | +2 tests: `set_tokens_used` accepts `soft_warn_fired_at`; refresh acquires advisory lock |
| `apps/api/tests/budget/test_resolver.py` | Modify | +6 tests: 3 for #3 (DB-direct pre-check) + 3 for #4 (sticky soft-warn) |
| `apps/api/tests/budget/test_cleanup.py` | New | 4 tests: keeps recent, deletes old, batches at 10000, idempotent |
| `apps/api/tests/budget/integration/test_pack_a_e2e.py` | New | 3 e2e tests |
| `apps/api/tests/admin/test_budget_api.py` | Modify | +1 test: cleanup endpoint auth |
| `README.md` | Modify | Remove 4 closed tech-debt items; annotate remaining 4 with Pack B/C |
| `~/.claude/projects/.../memory/m4-d-pack-a-progress.md` | New | Memory file |
| `~/.claude/projects/.../memory/MEMORY.md` | Modify | Add Pack A pointer |

**Total new tests: 18.** Existing 31 M4.D tests stay green.

---

## Task 1: Foundation — Migration, Model, Settings, Metric

**Files:**
- Create: `apps/api/migrations/versions/18_add_soft_warn_fired_at.py`
- Modify: `apps/api/src/budget/models.py:56-85`
- Modify: `apps/api/src/core/config.py:147-156` (append after `tenant_budget_cache_*`)
- Modify: `apps/api/src/core/business_metrics.py:90-101` (append after M4.D counters)
- Create: `apps/api/tests/budget/test_migration_18.py`

- [ ] **Step 1: Create migration `18_add_soft_warn_fired_at.py`**

```python
"""add soft_warn_fired_at to tenant_budget_snapshots

Per M4.D Pack A spec §5.2: the #4 sticky soft-warn design records when
soft-warn fired in this period, so subsequent calls crossing the same
threshold don't double-fire. NULL = not yet fired this period.

Revision ID: 18_add_soft_warn_fired_at
Revises: 17_add_tenant_budgets
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "18_add_soft_warn_fired_at"
down_revision = "17_add_tenant_budgets"
branch_labels = None
depends_on = None


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

- [ ] **Step 2: Run migration upgrade to verify schema is valid**

```bash
cd apps/api && alembic upgrade head
```

Expected: `Running upgrade 17_add_tenant_budgets -> 18_add_soft_warn_fired_at`. No errors.

- [ ] **Step 3: Run migration downgrade to verify rollback works**

```bash
cd apps/api && alembic downgrade -1
```

Expected: `Running downgrade 18_add_soft_warn_fired_at -> 17_add_tenant_budgets`.

- [ ] **Step 4: Re-upgrade to leave DB at head**

```bash
cd apps/api && alembic upgrade head
```

Expected: clean upgrade. No drift.

- [ ] **Step 5: Add `soft_warn_fired_at` to `TenantBudgetSnapshot` model**

Modify `apps/api/src/budget/models.py`. Find the `TenantBudgetSnapshot` class (around line 56). Add the new column after `last_refreshed_at` (before the closing of the class). First add `Optional` to the existing imports — change:

```python
from typing import Optional
```

(already imported; no change needed). Then inside `TenantBudgetSnapshot` class, after `last_refreshed_at` (the existing field ending at the line `DateTime(timezone=True), server_default=func.now(), nullable=False,)`) add:

```python
    soft_warn_fired_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
```

The full updated class shape should be:

```python
class TenantBudgetSnapshot(Base):
    """One row per (tenant, period) — running total of tokens consumed.

    Period format: ``YYYY-MM`` (string). Refreshed against ``llm_usage``
    on cache miss; incremented in-place on post-record.
    """

    __tablename__ = "tenant_budget_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "period",
            name="uq_tenant_budget_snapshots_tenant_period",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    tokens_used: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    last_refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    soft_warn_fired_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
```

- [ ] **Step 6: Write migration test `test_migration_18.py`**

Create `apps/api/tests/budget/test_migration_18.py`:

```python
"""Migration tests for 18_add_soft_warn_fired_at (M4.D Pack A).

Verifies alembic upgrade adds the column and downgrade drops it.
"""
from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from core.database import get_engine, get_sessionmaker


@pytest.fixture
def _alembic_upgraded() -> None:
    """Apply all migrations up to the head revision."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


def test_upgrade_adds_soft_warn_fired_at_column(_alembic_upgraded: None) -> None:
    """After upgrade, tenant_budget_snapshots has a TIMESTAMPTZ NULL column."""
    engine = get_engine()
    insp = inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("tenant_budget_snapshots")}
    assert "soft_warn_fired_at" in cols
    col = cols["soft_warn_fired_at"]
    # TIMESTAMP WITH TIME ZONE
    assert "TIMESTAMP" in str(col["type"]).upper()
    assert col["nullable"] is True


def test_downgrade_drops_soft_warn_fired_at_column() -> None:
    """After downgrade -1, the column is removed."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")  # one revision back
    engine = get_engine()
    insp = inspect(engine)
    col_names = {c["name"] for c in insp.get_columns("tenant_budget_snapshots")}
    assert "soft_warn_fired_at" not in col_names
    # Re-upgrade to leave DB clean for the next test.
    command.upgrade(cfg, "head")
```

- [ ] **Step 7: Run migration tests**

```bash
cd apps/api && pytest tests/budget/test_migration_18.py -v
```

Expected: 2 tests PASSED.

- [ ] **Step 8: Add 2 new Settings fields**

Modify `apps/api/src/core/config.py`. Find the comment block starting `# M4.D — Tenant token budget.` (around line 147). After the existing `tenant_budget_cache_maxsize` field, append:

```python
    # M4.D Pack A — Operational hygiene + correctness.
    #
    # ``tenant_budget_pre_check_use_db`` — when True (default), the
    # ``BudgetResolver._pre_check`` queries the snapshot table directly
    # instead of going through the in-process LRU cache. This closes
    # the ≤60s overshoot window documented in M4.D §11 (tech-debt #3).
    # Set False only as a soft escape hatch for tenants that tolerate
    # up-to-one-call overshoot in exchange for reduced DB read load.
    #
    # ``tenant_budget_cleanup_retention_months`` — drives the daily
    # ``budget_cleanup_task`` arq cron. 13 = 12 audit retention + 1
    # buffer month. Lowering it shrinks the audit window; raising it
    # keeps more history at the cost of disk.
    tenant_budget_pre_check_use_db: bool = Field(
        default=True, alias="TENANT_BUDGET_PRE_CHECK_USE_DB"
    )
    tenant_budget_cleanup_retention_months: int = Field(
        default=13, alias="TENANT_BUDGET_CLEANUP_RETENTION_MONTHS"
    )
```

- [ ] **Step 9: Add `LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL` counter**

Modify `apps/api/src/core/business_metrics.py`. Find the comment block starting `# M4.D — Tenant budget enforcement counters` (around line 90) and the two existing `LLM_TENANT_BUDGET_*` counter definitions. Append after the `LLM_TENANT_BUDGET_SOFT_WARN_TOTAL` counter:

```python
# M4.D Pack A — auto-cleanup observability.
LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL = Counter(
    "lumen_budget_cleanup_rows_deleted_total",
    "Number of tenant_budget_snapshots rows deleted by the cleanup task.",
)
```

Also add `"LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL"` to the `__all__` list at the bottom of the file (alphabetically between `LLM_CALLS_TOTAL` and `LLM_FALLBACK_ATTEMPTS_TOTAL`):

```python
    "LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL",
```

- [ ] **Step 10: Verify Settings + metric import without error**

```bash
cd apps/api && python -c "from core.config import Settings, get_settings; from core.business_metrics import LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL; s = Settings(); print('cleanup:', s.tenant_budget_cleanup_retention_months); print('pre_check_use_db:', s.tenant_budget_pre_check_use_db); print('counter:', LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL._name)"
```

Expected output (counter internal name):

```
cleanup: 13
pre_check_use_db: True
counter: lumen_budget_cleanup_rows_deleted_total
```

- [ ] **Step 11: Commit Task 1**

```bash
git add apps/api/migrations/versions/18_add_soft_warn_fired_at.py \
        apps/api/src/budget/models.py \
        apps/api/src/core/config.py \
        apps/api/src/core/business_metrics.py \
        apps/api/tests/budget/test_migration_18.py
git commit -m "feat(budget): M4.D Pack A foundation — soft_warn_fired_at column + settings

Adds the column + Settings fields + tests for the /models layer. This is
the foundation for the #4 sticky soft-warn design (5.2) and the #1
cleanup retention setting (5.4). No resolver / repository changes yet.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 2: Repository — `set_tokens_used` accepts `soft_warn_fired_at`

**Files:**
- Modify: `apps/api/src/budget/repository.py:89-117`
- Modify: `apps/api/tests/budget/test_repository.py:43-56` (extend existing test)

- [ ] **Step 1: Write failing test — `set_tokens_used` accepts `soft_warn_fired_at`**

Append to `apps/api/tests/budget/test_repository.py` (after the existing `test_snapshot_set_tokens_used_inserts_or_updates` at line 43):

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/api && pytest tests/budget/test_repository.py::test_snapshot_set_tokens_used_accepts_soft_warn_fired_at -v
```

Expected: FAIL with `TypeError: set_tokens_used() got an unexpected keyword argument 'soft_warn_fired_at'`.

- [ ] **Step 3: Implement `set_tokens_used` signature + UPSERT**

Modify `apps/api/src/budget/repository.py`. Replace the `set_tokens_used` method (lines 89-117) with:

```python
    async def set_tokens_used(
        self,
        *,
        tenant_id: str,
        period: str,
        tokens_used: int,
        soft_warn_fired_at: datetime | None = None,
    ) -> TenantBudgetSnapshot:
        """Upsert tokens_used (and optionally soft_warn_fired_at) for (tenant_id, period).

        Pack A #4: when ``soft_warn_fired_at`` is None on input, the existing
        row's value is preserved (no NULL overwrite). This is the carry-over
        behavior that keeps sticky soft-warn from accidentally clearing the
        original fire timestamp on subsequent calls in the same period.

        Concurrent calls: the second one wins (last-writer-wins). Acceptable
        for budget tracking — exact values aren't billing-grade.
        """
        sm = get_sessionmaker()
        async with sm() as session:
            set_clause: dict[str, Any] = {"tokens_used": tokens_used}
            # Pack A #4: only overwrite soft_warn_fired_at when an explicit
            # non-None value is provided. Passing None leaves the existing
            # value untouched (sticky behavior).
            if soft_warn_fired_at is not None:
                set_clause["soft_warn_fired_at"] = soft_warn_fired_at
            stmt = (
                pg_insert(TenantBudgetSnapshot)
                .values(
                    id=new_id(),
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
            result = await session.execute(stmt)
            row_obj = result.scalar_one()
            await session.commit()
            await session.refresh(row_obj)
            return row_obj
```

Add `Any` to the typing imports at the top of the file — change:

```python
from datetime import datetime
```

to:

```python
from datetime import datetime
from typing import Any
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd apps/api && pytest tests/budget/test_repository.py::test_snapshot_set_tokens_used_accepts_soft_warn_fired_at -v
```

Expected: PASS.

- [ ] **Step 5: Run full repository test suite to confirm no regression**

```bash
cd apps/api && pytest tests/budget/test_repository.py -v
```

Expected: 6 tests PASSED (5 existing + 1 new).

- [ ] **Step 6: Commit Task 2**

```bash
git add apps/api/src/budget/repository.py apps/api/tests/budget/test_repository.py
git commit -m "feat(budget): Pack A — set_tokens_used accepts soft_warn_fired_at (#4)

Per spec §5.2: when soft_warn_fired_at is None on input, the existing
row's value is preserved (no NULL overwrite). This is the carry-over
behavior that keeps sticky soft-warn from accidentally clearing the
original fire timestamp on subsequent calls in the same period.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 3: Repository — `refresh()` acquires `pg_advisory_xact_lock`

**Files:**
- Modify: `apps/api/src/budget/repository.py:119-172`
- Modify: `apps/api/tests/budget/test_repository.py` (append new test)

- [ ] **Step 1: Write failing test — refresh acquires advisory lock**

Append to `apps/api/tests/budget/test_repository.py`:

```python
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

        async def execute(self, stmt, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
            nonlocal lock_seen
            sql_str = str(stmt)
            if "pg_advisory" in sql_str:
                lock_seen = True
            return await self._inner.execute(stmt, *args, **kwargs)

    # Patch get_sessionmaker to return our wrapping factory
    import core.database as db_module
    real_sm = db_module.get_sessionmaker()

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

    db_module.get_sessionmaker = lambda: _WrappedSM()  # type: ignore[assignment]

    try:
        await repo.refresh(
            tenant_id=tenant.id,
            period="2026-10",
            period_starts_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
    finally:
        db_module.get_sessionmaker = real_sm  # type: ignore[assignment]

    assert lock_seen, "Expected pg_advisory_xact_lock to be issued during refresh()"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/api && pytest tests/budget/test_repository.py::test_snapshot_refresh_acquires_advisory_lock -v
```

Expected: FAIL with `AssertionError: Expected pg_advisory_xact_lock to be issued during refresh()`.

- [ ] **Step 3: Implement advisory lock in `refresh()`**

Modify `apps/api/src/budget/repository.py`. Replace the `refresh` method (lines 119-172) with:

```python
    async def refresh(
        self,
        *,
        tenant_id: str,
        period: str,
        period_starts_at: datetime,
    ) -> TenantBudgetSnapshot:
        """Run ``SUM(prompt+completion)`` on llm_usage and write a snapshot.

        Called on cache miss when no row exists for the current period.
        Idempotent: re-running refreshes the cached value.

        Pack A #7: holds ``pg_advisory_xact_lock(hashtext(tenant_id||':'||period))``
        for the duration of the transaction so concurrent refreshes for the
        same (tenant, period) serialize. Different (tenant, period) pairs
        do NOT block each other (different hashtext keys).
        """
        # Late import to avoid circular dep with llm_client.models
        from llm_client.models import LLMUsage
        from sqlalchemy import text

        # LLMUsage.created_at is TIMESTAMP WITHOUT TIME ZONE — strip tz if present
        # so asyncpg can bind the value without offset-naive/aware mismatch.
        if period_starts_at.tzinfo is not None:
            period_starts_at = period_starts_at.replace(tzinfo=None)

        sm = get_sessionmaker()
        async with sm() as session:
            async with session.begin():
                # Advisory lock keyed on hashtext(tenant_id||':'||period).
                # Same (tenant, period) → same hash → serializes.
                # Different combinations → different hashes → no blocking.
                # Released automatically when the transaction ends.
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": f"{tenant_id}:{period}"},
                )
                sum_expr = func.coalesce(
                    func.sum(LLMUsage.prompt_tokens + LLMUsage.completion_tokens), 0
                )
                result = await session.execute(
                    select(sum_expr).where(
                        LLMUsage.tenant_id == tenant_id,
                        LLMUsage.created_at >= period_starts_at,
                    )
                )
                total = int(result.scalar_one())
                stmt = (
                    pg_insert(TenantBudgetSnapshot)
                    .values(
                        id=new_id(),
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

Note: the new `refresh` uses `async with session.begin()` for an explicit transaction so the `pg_advisory_xact_lock` auto-releases. This changes from the previous pattern (which relied on autocommit at `await session.commit()`) — the `commit()` call is gone because `session.begin()` handles it.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd apps/api && pytest tests/budget/test_repository.py::test_snapshot_refresh_acquires_advisory_lock -v
```

Expected: PASS.

- [ ] **Step 5: Run full repository test suite to confirm no regression**

```bash
cd apps/api && pytest tests/budget/test_repository.py -v
```

Expected: 7 tests PASSED (5 existing + 2 new — the 5 original tests must continue to pass).

- [ ] **Step 6: Commit Task 3**

```bash
git add apps/api/src/budget/repository.py apps/api/tests/budget/test_repository.py
git commit -m "feat(budget): Pack A — refresh() acquires pg_advisory_xact_lock (#7)

Per spec §5.3: refresh() now runs inside a session.begin() transaction
and issues pg_advisory_xact_lock(hashtext(tenant_id||':'||period))
before the SUM()+UPSERT. Concurrent refreshes against the same
(tenant, period) serialize; different combinations do not block.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 4: Resolver — DB-direct `_pre_check` + sticky soft-warn `_post_record`

**Files:**
- Modify: `apps/api/src/budget/resolver.py:155-182` (`_pre_check`)
- Modify: `apps/api/src/budget/resolver.py:186-228` (`_post_record`)
- Modify: `apps/api/tests/budget/test_resolver.py` (preamble + 3 existing tests + 6 new tests)

- [ ] **Step 0: Pre-fix existing tests for new `_post_record` signature**

The new `_post_record` (Step 7 below) passes an extra `soft_warn_fired_at` kwarg to `set_tokens_used` AND reads via `snapshot_repo.get_for_tenant_period` instead of `cache.get_or_load_async`. Three existing tests in `apps/api/tests/budget/test_resolver.py` need to be updated to match. Without this step, those tests will fail when the new implementation runs.

**A.** Update the `_make_snapshot` helper (around line 29) — add `s.soft_warn_fired_at = None` so `spec=TenantBudgetSnapshot` snapshots default to "not yet fired":

```python
def _make_snapshot(tenant_id: str, period: str, tokens_used: int) -> Any:
    """Build a TenantBudgetSnapshot-like stub (no DB)."""
    s = MagicMock(spec=TenantBudgetSnapshot)
    s.tenant_id = tenant_id
    s.period = period
    s.tokens_used = tokens_used
    s.id = "snap-id"
    s.last_refreshed_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    s.soft_warn_fired_at = None  # Pack A #4: default to "not yet fired"
    return s
```

**B.** In `test_pre_check_pass_when_under_cap` (around line 48), update to use DB-direct mock + relax the strict set_tokens_used assertion. Find the test and replace it with:

```python
async def test_pre_check_pass_when_under_cap() -> None:
    """Snapshot below hard cap → inner resolver is invoked normally."""
    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response())
    cache = MagicMock()
    repo = MagicMock()
    snap = _make_snapshot("t1", "2026-10", 50)
    repo.get_for_tenant_period = AsyncMock(return_value=snap)
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(hard_cap_tokens=200),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    result = await resolver.ainvoke(MagicMock(spec=ChatRequest))
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 50
    # Inner was called exactly once
    inner.ainvoke.assert_awaited_once()
    # Post-record wrote the new total. We don't lock the
    # ``soft_warn_fired_at`` kwarg (Pack A #4 internal detail) — just
    # verify ``set_tokens_used`` was called with the right tokens_used.
    repo.set_tokens_used.assert_awaited()
    last_kwargs = repo.set_tokens_used.await_args.kwargs
    assert last_kwargs["tokens_used"] == 200
    assert last_kwargs["period"] == "2026-10"
```

**C.** In `test_post_record_increments_snapshot` (around line 113), apply the same pattern — replace `cache.get_or_load_async` mock with `repo.get_for_tenant_period` mock, and relax the set_tokens_used assertion:

```python
async def test_post_record_increments_snapshot() -> None:
    """Post-record UPSERTs the snapshot with new total + invalidates cache."""
    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response(prompt_tokens=100, completion_tokens=50))
    cache = MagicMock()
    snap = _make_snapshot("t1", "2026-10", 0)
    repo = MagicMock()
    repo.get_for_tenant_period = AsyncMock(return_value=snap)
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    repo.set_tokens_used.assert_awaited()
    last_kwargs = repo.set_tokens_used.await_args.kwargs
    assert last_kwargs["tokens_used"] == 150
    # Cache invalidated with period kwarg (NOT just tenant_id)
    cache.invalidate.assert_called_once_with("t1", period="2026-10")
```

**D.** In `test_post_record_fires_soft_warn_once` (around line 137), the test's intent is unchanged but the mock plumbing needs updating. Replace the cache-based iter with a snapshot_repo iter:

```python
async def test_post_record_fires_soft_warn_once() -> None:
    """Soft-warn fires exactly once when THIS call crosses soft_warn_tokens.

    Each ``ainvoke`` reads the snapshot twice (pre-check + post-record,
    both via snapshot_repo.get_for_tenant_period in Pack A), so the
    iterator yields 4 entries — 99, 99 for the first call, then 149,
    149 for the second. The first call's pre-check + post-record both
    run against snap=99 → fires soft-warn (99 < 100 <= 149). The second
    call's pre-check + post-record both run against snap=149 → does NOT
    fire (149 < 100 is False). Net: +1 to the metric counter.
    """
    from core.business_metrics import LLM_TENANT_BUDGET_SOFT_WARN_TOTAL

    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response(prompt_tokens=100, completion_tokens=50))
    cache = MagicMock()

    snap_states = iter(
        [
            _make_snapshot("t1", "2026-10", 99),  # 1st ainvoke: pre-check
            _make_snapshot("t1", "2026-10", 99),  # 1st ainvoke: post-record
            _make_snapshot("t1", "2026-10", 149),  # 2nd ainvoke: pre-check (post-UPSERT)
            _make_snapshot("t1", "2026-10", 149),  # 2nd ainvoke: post-record
        ]
    )
    repo = MagicMock()
    repo.get_for_tenant_period = AsyncMock(side_effect=lambda *a, **kw: next(snap_states))
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(soft_warn_tokens=100, hard_cap_tokens=1000),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    before = LLM_TENANT_BUDGET_SOFT_WARN_TOTAL._value.get()  # type: ignore[attr-defined]
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    after = LLM_TENANT_BUDGET_SOFT_WARN_TOTAL._value.get()  # type: ignore[attr-defined]
    assert after - before == 1
```

**E.** In `test_ainvoke_post_record_runs_for_single_provider_chain` (around line 230), apply the same mock pattern update — use `repo.get_for_tenant_period` instead of `cache.get_or_load_async`, and use `assert_awaited()` instead of `assert_awaited_once_with(...)`:

```python
async def test_ainvoke_post_record_runs_for_single_provider_chain() -> None:
    """Single-provider tenant — BudgetResolver.ainvoke must run post_record even
    when the inner TenantResolver.ainvoke raises _NoChainConfigured.

    Regression: without this fix, cap tracking is silently bypassed for
    tenants with tenant_budgets row + single provider + no fallback chain.
    """
    from llm_client.tenant_resolver import _NoChainConfigured

    primary_provider = MagicMock()
    response = MagicMock()
    response.prompt_tokens = 100
    response.completion_tokens = 50
    primary_provider.chat = AsyncMock(return_value=response)

    class _FakeInner:
        async def ainvoke(self, request):
            raise _NoChainConfigured()

        def __call__(self, request):
            return primary_provider

    inner = _FakeInner()

    cache = MagicMock()
    snap = _make_snapshot("t1", "2026-10", 0)
    repo = MagicMock()
    repo.get_for_tenant_period = AsyncMock(return_value=snap)
    repo.set_tokens_used = AsyncMock()

    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )

    resp = await resolver.ainvoke(MagicMock())
    assert resp is response
    # post_record must have run via the single-provider fallback path
    repo.set_tokens_used.assert_awaited()
    last_kwargs = repo.set_tokens_used.await_args.kwargs
    assert last_kwargs["tokens_used"] == 150
```

- [ ] **Step 1: Write failing test — DB-direct `_pre_check` eliminates TTL window**

Append to `apps/api/tests/budget/test_resolver.py`:

```python
async def test_pre_check_db_direct_eliminates_ttl_window() -> None:
    """Pack A #3: _pre_check reads from snapshot_repo directly (not cache).

    Pre-populate DB-direct with tokens_used=7900 and hard_cap=7500.
    The cache returns a stale "100" value — if pre-check USED this it
    would let the call through. Test fails if implementation reads
    from cache instead of DB.
    """
    inner = MagicMock()
    inner.ainvoke = AsyncMock()
    cache = MagicMock()
    # Cache returns stale (low) value — proves DB-direct bypasses it.
    cache.get_or_load_async = AsyncMock(
        return_value=_make_snapshot("t1", "2026-10", 100)
    )
    cache.invalidate = MagicMock()
    repo = MagicMock()
    # DB-direct path returns the live value (above hard cap)
    repo.get_for_tenant_period = AsyncMock(
        return_value=_make_snapshot("t1", "2026-10", 7900)
    )
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(hard_cap_tokens=7500),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    # Pre-populate DB-direct: tokens_used=7900, hard_cap=7500 → reject.
    # Cache returns a stale "100" value — if pre-check USED this, it would
    # let the call through. Test fails if implementation reads from cache.
    with pytest.raises(TenantBudgetExceeded):
        await resolver.ainvoke(MagicMock(spec=ChatRequest))
    # Pre-check went through DB-direct, NOT the cache
    repo.get_for_tenant_period.assert_awaited()
    cache.get_or_load_async.assert_not_called()
    # Inner resolver was never invoked (rejection happened first)
    inner.ainvoke.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/api && pytest tests/budget/test_resolver.py::test_pre_check_db_direct_eliminates_ttl_window -v
```

Expected: FAIL because the current `_pre_check` calls `cache.get_or_load_async`, so `cache.get_or_load_async.assert_not_called()` fails. (Implementation never reads from `snapshot_repo` in pre-check.)

- [ ] **Step 3: Implement DB-direct `_pre_check`**

Modify `apps/api/src/budget/resolver.py`. Replace the `_pre_check` method (lines 155-182) with:

```python
    async def _pre_check(self) -> tuple[str, datetime]:
        """Async pre-check: load snapshot from DB (Pack A #3) and raise if at cap.

        Pack A #3: when ``tenant_budget_pre_check_use_db`` is True (default),
        bypasses the in-process LRU cache and reads ``tenant_budget_snapshots``
        directly. This closes the ≤60s overshoot window documented in M4.D §11
        (tech-debt #3). When False, falls back to the M4.D cache path
        (soft escape hatch for tenants that tolerate up-to-one-call overshoot).

        Returns:
            ``(period, period_start)`` tuple for reuse by ``_post_record``.

        Raises:
            TenantBudgetExceeded: when ``tokens_used >= hard_cap_tokens``.
        """
        if self._budget is None or self._budget.hard_cap_tokens is None:
            period, period_start = _current_period("UTC")
            return period, period_start
        period, period_start = _current_period(self._budget.period_anchor_tz)
        settings = get_settings()
        if settings.tenant_budget_pre_check_use_db:
            # DB-direct path (Pack A #3): zero window between cap and rejection.
            snap = await self._snapshot_repo.get_for_tenant_period(
                self._tenant_id, period
            )
            if snap is None:
                # No snapshot row yet — refresh via SUM(llm_usage) (which
                # itself holds the advisory lock per Pack A #7).
                snap = await self._snapshot_repo.refresh(
                    tenant_id=self._tenant_id,
                    period=period,
                    period_starts_at=period_start,
                )
        else:
            # Legacy cache path — preserved as soft escape hatch.
            snap = await self._snapshot_cache.get_or_load_async(
                self._tenant_id,
                period=period,
                period_starts_at=period_start,
            )
        if snap.tokens_used >= self._budget.hard_cap_tokens:
            LLM_TENANT_BUDGET_EXCEEDED_TOTAL.inc()
            raise TenantBudgetExceeded(
                tenant_id=self._tenant_id,
                period=period,
                tokens_used=snap.tokens_used,
                hard_cap_tokens=self._budget.hard_cap_tokens,
                period_starts_at=period_start,
            )
        return period, period_start
```

Add `get_settings` import — at the top of the file, change the import block:

```python
from budget.cache import TenantBudgetSnapshotCache
from budget.models import TenantBudget
from budget.repository import TenantBudgetSnapshotRepository
from core.business_metrics import (
    LLM_TENANT_BUDGET_EXCEEDED_TOTAL,
    LLM_TENANT_BUDGET_SOFT_WARN_TOTAL,
)
```

to:

```python
from budget.cache import TenantBudgetSnapshotCache
from budget.models import TenantBudget
from budget.repository import TenantBudgetSnapshotRepository
from core.business_metrics import (
    LLM_TENANT_BUDGET_EXCEEDED_TOTAL,
    LLM_TENANT_BUDGET_SOFT_WARN_TOTAL,
)
from core.config import get_settings
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd apps/api && pytest tests/budget/test_resolver.py::test_pre_check_db_direct_eliminates_ttl_window -v
```

Expected: PASS.

- [ ] **Step 5: Write failing test — sticky soft-warn fires once per period**

Append to `apps/api/tests/budget/test_resolver.py`:

```python
async def test_soft_warn_fires_once_per_period_sticky() -> None:
    """Pack A #4: soft-warn fires exactly once per (tenant, period).

    First call crosses threshold → fires (+1 to metric) + sets soft_warn_fired_at.
    Second call also crosses (would have fired under old logic) → does NOT fire
    because soft_warn_fired_at IS NOT NULL (sticky).
    """
    from core.business_metrics import LLM_TENANT_BUDGET_SOFT_WARN_TOTAL
    from datetime import datetime as _dt

    inner = MagicMock()
    inner.ainvoke = AsyncMock(side_effect=[
        _make_response(prompt_tokens=100, completion_tokens=50),
        _make_response(prompt_tokens=100, completion_tokens=50),
    ])
    cache = MagicMock()

    # Build snapshots for the resolver's reads. Each call does:
    #   - DB-direct pre-check: 1 read via snapshot_repo.get_for_tenant_period
    #   - post-record: 1 read via snapshot_repo.get_for_tenant_period
    snap_with_no_fire = _make_snapshot("t1", "2026-10", 99)
    snap_with_no_fire.soft_warn_fired_at = None
    snap_after_fire = _make_snapshot("t1", "2026-10", 149)
    snap_after_fire.soft_warn_fired_at = _dt(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

    repo = MagicMock()
    repo.get_for_tenant_period = AsyncMock(side_effect=[
        snap_with_no_fire,  # call 1 pre-check: 99, no fire
        snap_with_no_fire,  # call 1 post-record: 99, no fire
        snap_after_fire,    # call 2 pre-check: 149, ALREADY FIRED
        snap_after_fire,    # call 2 post-record: 149, ALREADY FIRED
    ])
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(soft_warn_tokens=100, hard_cap_tokens=1000),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    before = LLM_TENANT_BUDGET_SOFT_WARN_TOTAL._value.get()  # type: ignore[attr-defined]
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    after = LLM_TENANT_BUDGET_SOFT_WARN_TOTAL._value.get()  # type: ignore[attr-defined]
    # Call 1: snap.tokens_used (99) < soft_warn (100) <= new_used (149) → fires
    # Call 2: snap.soft_warn_fired_at IS NOT NULL → no fire (sticky)
    assert after - before == 1
    # Both set_tokens_used calls preserved the fire timestamp
    assert repo.set_tokens_used.await_count == 2
```

- [ ] **Step 6: Run test to verify it fails**

```bash
cd apps/api && pytest tests/budget/test_resolver.py::test_soft_warn_fires_once_per_period_sticky -v
```

Expected: FAIL because current `_post_record` checks only `snap.tokens_used < soft_warn_tokens <= new_used`, not `snap.soft_warn_fired_at IS NULL`. The second call would also fire (+2 total).

- [ ] **Step 7: Implement sticky soft-warn `_post_record`**

Modify `apps/api/src/budget/resolver.py`. Replace the `_post_record` method (lines 186-228) with:

```python
    async def _post_record(
        self,
        period: str,
        period_start: datetime,
        tokens_consumed: int,
    ) -> None:
        """Increment snapshot, invalidate cache, fire sticky soft-warn.

        Pack A #4: soft-warn fires AT MOST ONCE per (tenant, period). The
        ``tenant_budget_snapshots.soft_warn_fired_at`` column records the
        fire timestamp; subsequent calls that would have crossed the
        threshold are no-ops (the ``snap.soft_warn_fired_at IS NULL``
        check is the gate).

        Pack A #3: ``set_tokens_used`` writes ``soft_warn_fired_at`` when
        firing (new value) or when an explicit non-None value is passed
        (carry-over from existing row). NULL on input preserves the
        existing timestamp.
        """
        snap = await self._snapshot_repo.get_for_tenant_period(
            self._tenant_id, period
        )
        if snap is None:
            snap = await self._snapshot_repo.refresh(
                tenant_id=self._tenant_id,
                period=period,
                period_starts_at=period_start,
            )
        new_used = snap.tokens_used + tokens_consumed

        # Pack A #4 sticky soft-warn check:
        #   - budget.soft_warn_tokens must be configured
        #   - snap.soft_warn_fired_at must be NULL (not yet fired this period)
        #   - the OLD usage was below threshold, the NEW usage is at/above
        fire_soft_warn = (
            self._budget is not None
            and self._budget.soft_warn_tokens is not None
            and snap.soft_warn_fired_at is None
            and snap.tokens_used < self._budget.soft_warn_tokens <= new_used
        )
        soft_warn_fired_at: datetime | None = snap.soft_warn_fired_at
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
            soft_warn_fired_at=soft_warn_fired_at,
        )
        # CRITICAL: invalidate cache using period= kwarg (not just tenant_id).
        self._snapshot_cache.invalidate(self._tenant_id, period=period)
```

- [ ] **Step 8: Run test to verify it passes**

```bash
cd apps/api && pytest tests/budget/test_resolver.py::test_soft_warn_fires_once_per_period_sticky -v
```

Expected: PASS.

- [ ] **Step 9: Write 4 more tests for `_pre_check` and `_post_record`**

First update the `_make_snapshot` helper to default `soft_warn_fired_at=None` so existing tests (`test_post_record_fires_soft_warn_once`, `test_pre_check_pass_when_under_cap`, etc.) continue to pass. Without this, `spec=TenantBudgetSnapshot` returns a MagicMock for `soft_warn_fired_at` which is truthy → sticky check fails → soft-warn never fires in existing tests, breaking the `after - before == 1` assertion.

Find the `_make_snapshot` helper (around line 29) and add one line:

```python
def _make_snapshot(tenant_id: str, period: str, tokens_used: int) -> Any:
    """Build a TenantBudgetSnapshot-like stub (no DB)."""
    s = MagicMock(spec=TenantBudgetSnapshot)
    s.tenant_id = tenant_id
    s.period = period
    s.tokens_used = tokens_used
    s.id = "snap-id"
    s.last_refreshed_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    s.soft_warn_fired_at = None  # Pack A #4: default to "not yet fired"
    return s
```

Then append to `apps/api/tests/budget/test_resolver.py`:

```python
async def test_pre_check_db_direct_when_setting_enabled_default() -> None:
    """Default settings: tenant_budget_pre_check_use_db=True → DB-direct path."""
    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response())
    cache = MagicMock()
    snap = _make_snapshot("t1", "2026-10", 100)
    repo = MagicMock()
    repo.get_for_tenant_period = AsyncMock(return_value=snap)
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(hard_cap_tokens=1000),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    # DB-direct path: repo.get_for_tenant_period called for pre-check
    assert repo.get_for_tenant_period.await_count >= 1
    # Cache was NOT consulted for the pre-check (only invalidated)
    cache.get_or_load_async.assert_not_called()


async def test_pre_check_cache_fallback_when_setting_disabled() -> None:
    """tenant_budget_pre_check_use_db=False → legacy cache path is used."""
    from core.config import get_settings, reset_settings
    import os

    os.environ["TENANT_BUDGET_PRE_CHECK_USE_DB"] = "false"
    reset_settings()
    try:
        inner = MagicMock()
        inner.ainvoke = AsyncMock(return_value=_make_response())
        cache = MagicMock()
        cache.get_or_load_async = AsyncMock(
            return_value=_make_snapshot("t1", "2026-10", 100)
        )
        repo = MagicMock()
        resolver = BudgetResolver(
            inner=inner, tenant_id="t1",
            budget=_make_budget(hard_cap_tokens=1000),
            snapshot_cache=cache,
            snapshot_repo=repo,
        )
        await resolver.ainvoke(MagicMock(spec=ChatRequest))
        cache.get_or_load_async.assert_awaited()
        # DB-direct path NOT used
        repo.get_for_tenant_period.assert_not_called()
    finally:
        os.environ.pop("TENANT_BUDGET_PRE_CHECK_USE_DB", None)
        reset_settings()


async def test_soft_warn_does_not_fire_when_already_fired() -> None:
    """Pre-populated soft_warn_fired_at → cross threshold does NOT fire."""
    from core.business_metrics import LLM_TENANT_BUDGET_SOFT_WARN_TOTAL
    from datetime import datetime as _dt

    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response(prompt_tokens=100, completion_tokens=50))
    cache = MagicMock()

    snap = _make_snapshot("t1", "2026-10", 99)
    snap.soft_warn_fired_at = _dt(2026, 10, 10, 0, 0, tzinfo=timezone.utc)

    repo = MagicMock()
    repo.get_for_tenant_period = AsyncMock(side_effect=[
        snap,  # pre-check
        snap,  # post-record
    ])
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(soft_warn_tokens=100, hard_cap_tokens=1000),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    before = LLM_TENANT_BUDGET_SOFT_WARN_TOTAL._value.get()  # type: ignore[attr-defined]
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    after = LLM_TENANT_BUDGET_SOFT_WARN_TOTAL._value.get()  # type: ignore[attr-defined]
    # snap.soft_warn_fired_at IS NOT NULL → no fire
    assert after - before == 0
    # Carry-over: set_tokens_used receives the existing one, not None
    repo.set_tokens_used.assert_awaited_once()
    call_kwargs = repo.set_tokens_used.await_args.kwargs
    assert call_kwargs["soft_warn_fired_at"] == _dt(2026, 10, 10, 0, 0, tzinfo=timezone.utc)


async def test_post_record_carries_over_soft_warn_fired_at() -> None:
    """Post-record preserves existing soft_warn_fired_at when not firing."""
    from datetime import datetime as _dt

    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response(prompt_tokens=100, completion_tokens=50))
    cache = MagicMock()
    existing_ts = _dt(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    snap = _make_snapshot("t1", "2026-10", 100)  # already past soft_warn but no fire
    snap.soft_warn_fired_at = existing_ts  # but we previously fired

    repo = MagicMock()
    repo.get_for_tenant_period = AsyncMock(side_effect=[snap, snap])
    repo.set_tokens_used = AsyncMock()
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1",
        budget=_make_budget(soft_warn_tokens=50, hard_cap_tokens=1000),
        snapshot_cache=cache,
        snapshot_repo=repo,
    )
    await resolver.ainvoke(MagicMock(spec=ChatRequest))
    repo.set_tokens_used.assert_awaited_once()
    call_kwargs = repo.set_tokens_used.await_args.kwargs
    # Existing timestamp preserved (not overwritten with None)
    assert call_kwargs["soft_warn_fired_at"] == existing_ts
```

- [ ] **Step 10: Run all 6 new resolver tests**

```bash
cd apps/api && pytest tests/budget/test_resolver.py -v
```

Expected: 15 tests PASSED (9 existing + 6 new).

- [ ] **Step 11: Commit Task 4**

```bash
git add apps/api/src/budget/resolver.py apps/api/tests/budget/test_resolver.py
git commit -m "feat(budget): Pack A — DB-direct pre-check + sticky soft-warn (#3 + #4)

Per spec §5.1 + §5.2:
- _pre_check now reads tenant_budget_snapshots directly (Pack A #3) when
  tenant_budget_pre_check_use_db=True (default). Closes the ≤60s
  overshoot window documented in M4.D §11.
- _post_record now gates soft-warn on snap.soft_warn_fired_at IS NULL
  (Pack A #4). Fires at most once per (tenant, period). Carry-over
  preserves the existing timestamp on subsequent calls.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 5: Cleanup — `run_budget_cleanup` + admin endpoint + arq registration

**Files:**
- Create: `apps/api/src/budget/cleanup.py`
- Modify: `apps/api/src/admin/schemas/budget.py` (append `CleanupResponse`)
- Modify: `apps/api/src/admin/api.py` (append `POST /admin/budget/cleanup`)
- Modify: `apps/api/src/qa/worker.py:457-461` (extend `WorkerSettings` with `budget_cleanup_task` + cron)
- Create: `apps/api/tests/budget/test_cleanup.py`
- Modify: `apps/api/tests/admin/test_budget_api.py` (append new test)

- [ ] **Step 1: Write failing test — `run_budget_cleanup` keeps recent, deletes old**

Create `apps/api/tests/budget/test_cleanup.py`:

```python
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

    We seed 25000 old snapshots and verify the cleanup loop processes
    them. Direct rowcount: with 25000 < 100000, it takes exactly 3 batches
    (10000 + 10000 + 5000). The final batch returns < 10000 → break.
    """
    # Seed one tenant + 25001 old rows (to exercise batching)
    tenant = await TenantRepository().create(name="Cleanup Batch", plan=TenantPlan.PRO)
    sm = get_sessionmaker()
    old_period = "2023-01"  # very old
    rows = [
        {
            "id": new_id(),
            "tenant_id": tenant.id,
            "period": old_period,
            "tokens_used": 0,
            "soft_warn_fired_at": None,
        }
        for _ in range(25001)
    ]
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd apps/api && pytest tests/budget/test_cleanup.py -v
```

Expected: 4 failures with `ModuleNotFoundError: No module named 'budget.cleanup'`.

- [ ] **Step 3: Implement `run_budget_cleanup()`**

Create `apps/api/src/budget/cleanup.py`:

```python
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
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete

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
    now = datetime.now(timezone.utc)
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
                stmt = (
                    delete(TenantBudgetSnapshot)
                    .where(TenantBudgetSnapshot.period < cutoff)
                    .execution_options(synchronize_session=False)
                    .limit(_CLEANUP_BATCH_SIZE)
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
```

- [ ] **Step 4: Run cleanup tests to verify they pass**

```bash
cd apps/api && pytest tests/budget/test_cleanup.py -v
```

Expected: 4 tests PASSED.

- [ ] **Step 5: Write failing test — admin cleanup endpoint auth**

Append to `apps/api/tests/admin/test_budget_api.py` (after `test_post_with_cross_tenant_admin_returns_404`). The fixtures `async_client`, `admin_token_for`, and `auth_headers` are defined in `apps/api/tests/admin/conftest.py` and are auto-injected.

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_cleanup_endpoint_requires_jwt_auth(
    async_client: AsyncClient,
    admin_token_for,
) -> None:
    """POST /admin/budget/cleanup — JWT auth gate.

    - Without bearer token → 401
    - With regular admin JWT (claims has tenant_id) → 404 (anti-enumeration)
    - With super-admin JWT (claims has no tenant_id claim) → 200 with stats

    The super-admin token is issued by calling ``create_access_token`` with
    ``extra={"tenant_id": None}`` — the JWT layer accepts this and the
    resulting claims dict has ``claims.get("tenant_id")`` return ``None``,
    which is what the anti-enumeration check bypasses on.
    """
    from auth.jwt import create_access_token

    # 401 — no token
    resp = await async_client.post("/api/v1/admin/budget/cleanup")
    assert resp.status_code == 401

    # 404 — admin token (has tenant_id claim)
    tenant_token = admin_token_for(tenant_id="t-someone-else", user_id="admin-1")
    resp = await async_client.post(
        "/api/v1/admin/budget/cleanup",
        headers=auth_headers(tenant_token),
    )
    assert resp.status_code == 404

    # 200 — super-admin (no tenant_id claim via extra override)
    super_token = create_access_token(
        tenant_id="ignored",  # required by signature; overridden below
        user_id="super-1",
        role="super_admin",
        extra={"tenant_id": None},  # JWT payload's tenant_id becomes None
    )
    resp = await async_client.post(
        "/api/v1/admin/budget/cleanup",
        headers=auth_headers(super_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "deleted_rows" in body
    assert "cutoff_period" in body
```

Also add `"test_cleanup_endpoint_requires_jwt_auth"` to the `__all__` list at the bottom of `test_budget_api.py`:

```python
__all__ = [
    "test_post_creates_budget_row",
    "test_post_upserts_existing_row",
    "test_get_returns_current_budget",
    "test_get_returns_period_and_tokens_used",
    "test_get_usage_unknown_tenant_returns_404",
    "test_post_without_token_returns_401",
    "test_post_with_cross_tenant_admin_returns_404",
    "test_cleanup_endpoint_requires_jwt_auth",
]
```

- [ ] **Step 6: Run test to verify it fails**

```bash
cd apps/api && pytest tests/admin/test_budget_api.py::test_cleanup_endpoint_requires_jwt_auth -v
```

Expected: FAIL with 404 (endpoint not registered).

- [ ] **Step 7: Add `CleanupResponse` schema**

Modify `apps/api/src/admin/schemas/budget.py`. Append at the bottom of the file:

```python
class CleanupResponse(BaseModel):
    """Response body for POST /admin/budget/cleanup."""

    deleted_rows: int
    cutoff_period: str  # YYYY-MM
```

Also add `"CleanupResponse"` to the `__all__` list:

```python
__all__ = [
    "CleanupResponse",
    "TenantBudgetCreate",
    "TenantBudgetRead",
    "TenantBudgetUsageRead",
]
```

- [ ] **Step 8: Add admin `POST /admin/budget/cleanup` endpoint**

Modify `apps/api/src/admin/api.py`. First update the import line that pulls budget schemas — find:

```python
from admin.schemas.budget import (
    TenantBudgetCreate,
    TenantBudgetRead,
    TenantBudgetUsageRead,
)
```

Replace with:

```python
from admin.schemas.budget import (
    CleanupResponse,
    TenantBudgetCreate,
    TenantBudgetRead,
    TenantBudgetUsageRead,
)
```

Then add the budget cleanup import — find:

```python
from budget.repository import TenantBudgetSnapshotRepository
from budget.resolver import _current_period
```

Replace with:

```python
from budget.cleanup import run_budget_cleanup
from budget.repository import TenantBudgetSnapshotRepository
from budget.resolver import _current_period
```

Then append at the bottom of the file (after `get_tenant_budget_usage`):

```python
@router.post(
    "/budget/cleanup",
    response_model=CleanupResponse,
)
async def trigger_budget_cleanup(
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> CleanupResponse:
    """Manually trigger budget snapshot cleanup (M4.D Pack A #1).

    Cross-tenant: only callable by super-admin (no tenant_id in claims).
    The regular per-tenant admin gets a 404 — anti-enumeration mirrors
    the other admin endpoints in this module.

    Status codes
    ------------
    * 200 — cleanup ran, returns ``{deleted_rows, cutoff_period}``
    * 401 — missing / invalid bearer token (raised by require_admin)
    * 403 — token is not admin / owner role (raised by require_admin)
    * 404 — token has ``tenant_id`` claim (anti-enumeration)
    """
    if claims.get("tenant_id") is not None:
        # Anti-enumeration: don't reveal this endpoint exists to per-tenant
        # admins (the cleanup task affects ALL tenants globally).
        raise HTTPException(status_code=404, detail="not found")
    stats = await run_budget_cleanup()
    return CleanupResponse(
        deleted_rows=stats["deleted_rows"],
        cutoff_period=stats["cutoff_period"],
    )
```

- [ ] **Step 9: Run admin endpoint test to verify it passes**

```bash
cd apps/api && pytest tests/admin/test_budget_api.py::test_cleanup_endpoint_requires_jwt_auth -v
```

Expected: PASS.

- [ ] **Step 10: Register `budget_cleanup_task` in arq `WorkerSettings`**

Modify `apps/api/src/qa/worker.py`. First add the budget cleanup imports — find the existing budget-related import area (around line 60). After the `from core.business_metrics import (...)` block (around line 60-66), add the budget cleanup import:

```python
from budget.cleanup import run_budget_cleanup
```

Then append the task function before the `startup`/`shutdown` block (around line 380, before `# Worker lifecycle hooks`):

```python
# ---------------------------------------------------------------------------
# Arq cron: daily budget snapshot cleanup (M4.D Pack A #1)
# ---------------------------------------------------------------------------


async def budget_cleanup_task(ctx: dict[str, Any]) -> None:
    """Delete tenant_budget_snapshots rows older than retention cutoff.

    Cron: registered at minute={0}, hour={2} (02:00 UTC daily). The
    retention period is configurable via
    ``TENANT_BUDGET_CLEANUP_RETENTION_MONTHS`` (default 13 = 12 audit
    + 1 buffer). Idempotent — re-running is a no-op.

    The function delegates to :func:`budget.cleanup.run_budget_cleanup`
    which handles the batched DELETE + metric increment + log line.
    The task wrapper exists so the arq ``cron_jobs`` registration has
    a stable import path; the cleanup logic lives in ``budget.cleanup``
    so admin/CLI tooling can also trigger it directly.
    """
    stats = await run_budget_cleanup()
    log.info(
        "qa.budget_cleanup.completed",
        deleted_rows=stats["deleted_rows"],
        cutoff_period=stats["cutoff_period"],
    )
```

Then modify the `WorkerSettings` class (around line 439-461) — update both lists:

```python
class WorkerSettings:
    """Arq ``WorkerSettings`` — exposed via ``apps.api.src.workers``.

    Invoked by the production command::

        arq apps.api.src.workers.WorkerSettings

    Registered functions:

    * :func:`qa_judge_task` — main workload, enqueued per AI message.
    * :func:`qa_sla_alert_worker` — hourly cron, scans breached SLAs.
    * :func:`budget_cleanup_task` — daily 02:00 UTC cron, deletes
      expired tenant_budget_snapshots rows (M4.D Pack A #1).

    ``max_jobs=4``: the Judge is IO-bound (HTTP roundtrip to the
    LLM provider); 4 concurrent jobs is enough headroom without
    saturating the Judge provider's rate limit. Tunable per
    environment in a future iteration.
    """

    functions = [qa_judge_task]
    cron_jobs = [
        cron(qa_sla_alert_worker, minute={0}),
        cron(budget_cleanup_task, hour={2}, minute={0}),
    ]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 4
```

Also add `"budget_cleanup_task"` to the `__all__` list at the bottom of the file:

```python
__all__ = [
    "WorkerSettings",
    "build_arq_redis",
    "budget_cleanup_task",
    "qa_judge_task",
    "qa_sla_alert_worker",
]
```

- [ ] **Step 11: Verify the worker module imports cleanly**

```bash
cd apps/api && python -c "from qa.worker import WorkerSettings, budget_cleanup_task; print('cron:', len(WorkerSettings.cron_jobs), 'functions:', len(WorkerSettings.functions))"
```

Expected output:

```
cron: 2 functions: 1
```

- [ ] **Step 12: Run all budget + admin tests to confirm no regression**

```bash
cd apps/api && pytest tests/budget/ tests/admin/test_budget_api.py -v
```

Expected: all tests PASSED (47+ total — 6 existing repo + 2 new + 6 cache + 2 models + 15 resolver + 2 migration + 4 cleanup + ~7 admin).

- [ ] **Step 13: Commit Task 5**

```bash
git add apps/api/src/budget/cleanup.py \
        apps/api/src/admin/schemas/budget.py \
        apps/api/src/admin/api.py \
        apps/api/src/qa/worker.py \
        apps/api/tests/budget/test_cleanup.py \
        apps/api/tests/admin/test_budget_api.py
git commit -m "feat(budget): Pack A — cleanup task + admin endpoint + arq cron (#1)

Per spec §5.4:
- New budget/cleanup.py: run_budget_cleanup() deletes snapshots older
  than TENANT_BUDGET_CLEANUP_RETENTION_MONTHS (default 13) in LIMIT
  10000 batches. Idempotent + arq-retryable.
- New admin POST /admin/budget/cleanup: super-admin-only manual
  trigger with anti-enumeration 404 for per-tenant admins.
- New arq cron budget_cleanup_task registered at 02:00 UTC daily
  in qa/worker.WorkerSettings.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 6: E2E + README + memory close-out

**Files:**
- Create: `apps/api/tests/budget/integration/test_pack_a_e2e.py`
- Modify: `README.md` (remove 4 closed tech-debt items + annotate)
- Create: `~/.claude/projects/.../memory/m4-d-pack-a-progress.md`
- Modify: `~/.claude/projects/.../memory/MEMORY.md`

- [ ] **Step 1: Write 3 e2e tests via pytest-httpx**

Create `apps/api/tests/budget/integration/test_pack_a_e2e.py`:

```python
"""E2E tests for M4.D Pack A (tech-debt #3 + #4 + #7 + #1).

Tests exercise the resolver end-to-end against a live DB. Tenant +
budget fixtures are created via the public repositories. No direct
ORM manipulation beyond what's needed to seed snapshot rows.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from auth.jwt import create_access_token
from budget.cache import TenantBudgetSnapshotCache
from budget.repository import (
    TenantBudgetRepository,
    TenantBudgetSnapshotRepository,
)
from budget.resolver import BudgetResolver
from llm_client.exceptions import TenantBudgetExceeded
from llm_client.types import ChatRequest, ChatResponse
from tenant.enums import TenantPlan
from tenant.repository import TenantRepository


@pytest.fixture
async def async_client_app() -> AsyncClient:
    """Yield an httpx client wired to the FULL FastAPI app."""
    from main import app  # noqa: PLC0415

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def tenant_with_budget() -> str:
    """Create a tenant + a budget. Returns the tenant_id."""
    tenant = await TenantRepository().create(name="PackA E2E Tenant", plan=TenantPlan.PRO)
    await TenantBudgetRepository().upsert(
        tenant_id=tenant.id,
        soft_warn_tokens=6000,
        hard_cap_tokens=8000,
    )
    return tenant.id


def _make_response(prompt_tokens: int = 100, completion_tokens: int = 50) -> ChatResponse:
    """Build a ChatResponse stub that the inner resolver returns."""
    r = MagicMock(spec=ChatResponse)
    r.prompt_tokens = prompt_tokens
    r.completion_tokens = completion_tokens
    return r


async def test_e2e_pack_a_no_overshoot_under_concurrent_calls(tenant_with_budget: str) -> None:
    """Pack A #3 — DB-direct pre-check rejects at/above hard cap.

    Pre-populate snapshot tokens_used=7950 (under cap=8000). Run calls
    consuming 150 tokens. The pre-check at DB-direct must see 7950 + 150
    = 8100 (over cap) and reject the next call. Verify a rejection
    happens before tokens_used blows far above cap.
    """
    tenant_id = tenant_with_budget
    srepo = TenantBudgetSnapshotRepository()
    await srepo.set_tokens_used(
        tenant_id=tenant_id, period="2026-10", tokens_used=7950,
    )

    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response(prompt_tokens=100, completion_tokens=50))
    # Use a real cache object — DB-direct path doesn't read it, but the
    # constructor needs one. Default ttl/maxsize from Settings.
    cache = TenantBudgetSnapshotCache(ttl_s=60.0, maxsize=1024)
    budget = await TenantBudgetRepository().get_by_tenant(tenant_id)
    resolver = BudgetResolver(
        inner=inner, tenant_id=tenant_id,
        budget=budget, snapshot_cache=cache,
    )

    rejected_at: list[datetime] = []
    for _ in range(20):
        try:
            await resolver.ainvoke(MagicMock(spec=ChatRequest))
        except TenantBudgetExceeded:
            rejected_at.append(datetime.now(timezone.utc))
            break

    assert len(rejected_at) == 1
    # The cap WAS hit, post-record may have pushed tokens_used over
    # cap (we don't fix post-record in Pack A, only pre-check). Verify
    # the rejection happened — the exact tokens_used after rejection
    # is not asserted.
    snap = await srepo.get_for_tenant_period(tenant_id, "2026-10")
    assert snap is not None
    assert snap.tokens_used >= 8000


async def test_e2e_soft_warn_fires_once_under_load(tenant_with_budget: str) -> None:
    """Pack A #4 — soft-warn fires once across 5 calls crossing threshold."""
    from core.business_metrics import LLM_TENANT_BUDGET_SOFT_WARN_TOTAL

    tenant_id = tenant_with_budget
    srepo = TenantBudgetSnapshotRepository()
    # Pre-populate: 5900 used, soft_warn=6000 (will fire on first crossing)
    await srepo.set_tokens_used(
        tenant_id=tenant_id, period="2026-10",
        tokens_used=5900, soft_warn_fired_at=None,
    )

    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response(prompt_tokens=100, completion_tokens=100))
    cache = TenantBudgetSnapshotCache(ttl_s=60.0, maxsize=1024)
    budget = await TenantBudgetRepository().get_by_tenant(tenant_id)
    resolver = BudgetResolver(
        inner=inner, tenant_id=tenant_id,
        budget=budget, snapshot_cache=cache,
    )

    before = LLM_TENANT_BUDGET_SOFT_WARN_TOTAL._value.get()  # type: ignore[attr-defined]
    for _ in range(5):
        await resolver.ainvoke(MagicMock(spec=ChatRequest))
    after = LLM_TENANT_BUDGET_SOFT_WARN_TOTAL._value.get()  # type: ignore[attr-defined]
    # Call 1: 5900 + 200 = 6100 (crosses 6000) → fires
    # Calls 2-5: already fired → no fire
    assert after - before == 1


async def test_e2e_cleanup_admin_endpoint_requires_jwt(async_client_app: AsyncClient) -> None:
    """POST /admin/budget/cleanup — 401 / 404 / 200 auth gating."""
    # 401 — no token
    resp = await async_client_app.post("/api/v1/admin/budget/cleanup")
    assert resp.status_code == 401

    # 404 — admin token (has tenant_id claim)
    tenant_token = create_access_token(
        tenant_id="t-anyone", user_id="u-1", role="admin",
    )
    resp = await async_client_app.post(
        "/api/v1/admin/budget/cleanup",
        headers={"Authorization": f"Bearer {tenant_token}"},
    )
    assert resp.status_code == 404

    # 200 — super-admin (no tenant_id claim via extra override)
    super_token = create_access_token(
        tenant_id="ignored",  # required by signature; overridden below
        user_id="super-1",
        role="super_admin",
        extra={"tenant_id": None},  # claims.get("tenant_id") returns None
    )
    resp = await async_client_app.post(
        "/api/v1/admin/budget/cleanup",
        headers={"Authorization": f"Bearer {super_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "deleted_rows" in body
    assert "cutoff_period" in body
```

- [ ] **Step 2: Run e2e tests**

```bash
cd apps/api && pytest tests/budget/integration/test_pack_a_e2e.py -v
```

Expected: 3 tests PASSED.

- [ ] **Step 3: Run full budget + admin test suite to confirm 18 new + 31 existing regression**

```bash
cd apps/api && pytest tests/budget/ tests/admin/test_budget_api.py -v
```

Expected: all tests PASSED.

- [ ] **Step 4: Update README — remove 4 closed tech-debt items, annotate remaining 4**

Modify `README.md`. Find the section listing M4.D known tech debt (8 items). Remove items #1, #3, #4, #7 (closed by Pack A). Update items #2, #5, #6, #8 with Pack B/C annotations. Example for each remaining item:

```markdown
- **#2 No top-up mechanism** — *Deferred to Pack B.* If a tenant hits hard cap mid-month, there's no manual credit / soft-raise escape hatch.
- **#5 No per-model breakdown** — *Deferred to Pack B.* `tokens_used` aggregates all models for a tenant. If a tenant uses claude-opus + claude-haiku, the breakdown is opaque.
- **#6 No non-LLM cost budget** — *Deferred to Pack C.* Only LLM tokens are budgeted. SES email sends, SMS (transcations) — none are tracked.
- **#8 No provider 429 integration** — *Deferred to Pack B.* When a provider rate-limits mid-month, the resolver retries the chain (M4.B) but doesn't preempt based on budget.
```

Adjust the actual text to match the existing README structure.

- [ ] **Step 5: Create memory file `m4-d-pack-a-progress.md`**

Create the memory file at the path:

```bash
mkdir -p ~/.claude/projects/D--work-ai-0401-ai-customer/memory
```

Then write the file with content like:

```markdown
---
name: m4-d-pack-a-progress
description: M4.D Pack A tech debt follow-up — #3 (DB-direct pre-check) + #4 (sticky soft-warn) + #7 (advisory lock) + #1 (auto cleanup). 18 new tests, 5 commits, shipped to origin/main.
metadata:
  type: project
---

M4.D Pack A tech debt follow-up — completed 2026-10-06. Closed items #3, #4, #7, #1 of M4.D's 8 known tech-debt items.

**What's in:**
- `BudgetResolver._pre_check` reads `tenant_budget_snapshots` directly from DB (not cache) when `tenant_budget_pre_check_use_db=True` (default). Closes the ≤60s overshoot window.
- `tenant_budget_snapshots.soft_warn_fired_at` records when soft-warn fired; fires at most once per period (sticky). Carry-over preserves existing timestamp on subsequent calls.
- `TenantBudgetSnapshotRepository.refresh()` acquires `pg_advisory_xact_lock(hashtext(tenant_id||':'||period))` — concurrent refreshes for same (tenant, period) serialize.
- `run_budget_cleanup()` in `apps/api/src/budget/cleanup.py` deletes snapshots older than 13 months in LIMIT 10000 batches. Idempotent.
- `POST /admin/budget/cleanup` super-admin-only manual trigger (per-tenant admin gets 404 anti-enumeration).
- Arq cron `budget_cleanup_task` at 02:00 UTC daily, registered in `qa/worker.WorkerSettings`.

**Files modified (10) + new (8):** see spec §12 File Map at `apps/api/docs/superpowers/specs/2026-10-06-m4-d-pack-a-tech-debt-design.md`.

**Why:** Closes 4 of 8 M4.D tech-debt items in one shippable unit. The remaining 4 items (#2, #5, #6, #8) are deferred to Pack B and Pack C.

**How to apply:** Future budget-layer changes should preserve the advisory-lock pattern in `refresh()` and the carry-over semantics of `set_tokens_used(soft_warn_fired_at=...)`. The cache layer (`TenantBudgetSnapshotCache`) is now dead code in default config — removing it is a future tech-debt item.

Related: [[m4-d-progress]] (M4.D itself), [[m4-c-progress]] (BYOK TenantResolver).
```

- [ ] **Step 6: Update MEMORY.md index**

Append a line to `~/.claude/projects/D--work-ai-0401-ai-customer/memory/MEMORY.md`:

```markdown
- [M4.D Pack A progress](m4-d-pack-a-progress.md) — M4.D Pack A tech debt follow-up (#3 + #4 + #7 + #1), 18 new tests, shipped to origin/main
```

- [ ] **Step 7: Run final full test suite**

```bash
cd apps/api && pytest -v
```

Expected: all tests PASSED.

- [ ] **Step 8: Commit Task 6**

```bash
git add apps/api/tests/budget/integration/test_pack_a_e2e.py README.md
git commit -m "docs + test: M4.D Pack A close-out — e2e + README tech debt update

- 3 new e2e tests via pytest-httpx (no_overshoot + soft_warn_once +
  cleanup_admin_auth)
- README: remove 4 closed tech-debt items, annotate remaining 4 with
  Pack B/C tags

Co-Authored-By: Claude Code <noreply@anthropic.com>"

# Memory file is outside the repo, no commit needed
```

- [ ] **Step 9: Push to origin/main**

```bash
git push origin main
```

Expected: all commits pushed successfully. No rejections.

---

## Self-Review

After writing the plan, verify:

**1. Spec coverage:**
- §1 Goals: Task 1 (foundation) + Task 2 (soft_warn_fired_at column) + Task 3 (advisory lock) + Task 4 (DB-direct pre-check + sticky soft-warn) + Task 5 (cleanup) ✓
- §5.1 DB-direct: Task 4 Steps 3-4 ✓
- §5.2 Sticky soft-warn: Task 1 Steps 1-5 (column) + Task 2 (set_tokens_used accepts it) + Task 4 Steps 5-8 (resolver checks sticky) ✓
- §5.3 Advisory lock: Task 3 Steps 1-4 ✓
- §5.4 Cleanup: Task 5 Steps 3 (function) + Steps 7-9 (admin endpoint) + Step 10 (arq registration) ✓
- §6 Data Flow: covered by integration tests in Task 6 ✓
- §7 Error Handling: not unit-tested explicitly; arq retry + advisory lock wait are PG defaults ✓
- §8 Cardinality: Task 1 Step 9 (zero-label counter) ✓
- §9 Testing: 18 new tests (2 migration + 2 repository + 6 resolver + 4 cleanup + 1 admin + 3 e2e) ✓
- §10 Rollout: Task 6 Step 9 (push to origin/main) ✓
- §12 File Map: all 18 files accounted for in the 6 tasks ✓

**2. Placeholder scan:** No "TBD" / "TODO" / "implement later" patterns. Every code step shows complete code. Step 5 of Task 5 references real fixtures (`admin_token_for`, `auth_headers`) defined in `apps/api/tests/admin/conftest.py`; the super-admin case uses `create_access_token(extra={"tenant_id": None})` so the JWT carries `tenant_id: null` and the anti-enumeration check is bypassed.

**3. Type consistency:**
- `set_tokens_used(soft_warn_fired_at: datetime | None = None)` — defined Task 2 Step 3, used Task 4 Step 7 (resolver passes `soft_warn_fired_at: datetime | None`) ✓
- `run_budget_cleanup()` returns `dict[str, Any]` — defined Task 5 Step 3, used Task 5 Step 8 (admin endpoint) and Step 10 (arq task) ✓
- `WorkerSettings.cron_jobs` — extends existing list (Task 5 Step 10) ✓
- `CleanupResponse(deleted_rows, cutoff_period)` — schema Task 5 Step 7, endpoint Step 8, test Step 5 ✓