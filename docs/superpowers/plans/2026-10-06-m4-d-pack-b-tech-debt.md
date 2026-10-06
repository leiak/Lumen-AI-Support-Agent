# M4.D Pack B — Tech Debt Follow-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close 3 of 4 deferred M4.D tech-debt items by adding a manual credit-grant flow for super_admin (#2), a read-only per-model breakdown with 30s cache (#5), and a post-mortem provider-429 budget gate (#8).

**Architecture:** All 3 items compose within the existing M4.A/B/C/D resolver chain. `BudgetResolver._compute_effective_cap(period)` extends Pack A's pre-check by adding `SUM(tenant_budget_credits.tokens WHERE period=current)`. `PerModelBreakdownCache` (in-process dict, 30s TTL) backs a new `?breakdown=true` query on the snapshot endpoint. `TenantBudgetRateLimited` exception replaces a propagated 429 when the remaining budget after a chain-exhausting 429 is below a configurable token threshold. `FallbackResolver` and Pack A logic untouched.

**Tech Stack:** Python 3.11+, Pydantic v2, SQLAlchemy 2.0 async ORM, FastAPI, Alembic, Prometheus client, pytest-httpx.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `apps/api/migrations/versions/19_add_tenant_budget_credits.py` | New | Migration: create `tenant_budget_credits` table |
| `apps/api/src/budget/models.py` | Modify | Add `TenantBudgetCredit` ORM |
| `apps/api/src/budget/credits.py` | New | `CreditService` (grant + sum_for_period) |
| `apps/api/src/budget/per_model.py` | New | `PerModelBreakdownCache` + `PerModelService` |
| `apps/api/src/budget/exceptions.py` | New | `TenantBudgetRateLimited` exception |
| `apps/api/src/budget/resolver.py` | Modify | `_compute_effective_cap`, `_maybe_raise_budget_gate`, accept `credit_service` + `per_model_cache` deps |
| `apps/api/src/admin/schemas/budget.py` | Modify | Add `CreditRequest/Response`, `BreakdownItem`, extend `BudgetSnapshotResponse` |
| `apps/api/src/admin/api.py` | Modify | Add `POST/GET /admin/tenants/{tid}/credits`, extend `/admin/tenants/{tid}/budget/usage` snapshot endpoint, register exception handler |
| `apps/api/src/main.py` | Modify | Register `@app.exception_handler(TenantBudgetRateLimited)` |
| `apps/api/src/core/config.py` | Modify | Add 2 fields: `tenant_budget_429_skip_threshold_tokens` + `tenant_budget_per_model_cache_ttl_seconds` |
| `apps/api/src/core/business_metrics.py` | Modify | Add `LLM_BUDGET_GATE_TOTAL` counter |
| `apps/api/tests/budget/test_migration_19.py` | New | +2 tests: upgrade creates table, downgrade drops |
| `apps/api/tests/budget/test_credits.py` | New | +9 tests: grant validations, sum_for_period, effective_cap, API auth |
| `apps/api/tests/budget/test_per_model.py` | New | +9 tests: cache hit/miss/TTL/invalidate, breakdown endpoint, anti-enumeration |
| `apps/api/tests/budget/test_budget_gate.py` | New | +8 tests: gate fire/no-fire/no-post-record/cache-invalidate/metric/disabled/uses-effective-cap/handler |
| `apps/api/tests/budget/integration/test_pack_b_e2e.py` | New | +1 e2e test |
| `apps/api/tests/admin/test_budget_api.py` | Modify | +2 tests: credits POST/GET super_admin only |
| `README.md` | Modify | Remove #2/#5/#8 from M4.D tech debt list; add 1 known-limitation note |
| `~/.claude/projects/.../memory/m4-d-pack-b-progress.md` | New | Memory file |
| `~/.claude/projects/.../memory/MEMORY.md` | Modify | Add Pack B pointer |

**Total new tests: 27.** Existing 49 M4.D tests (Pack A: 18 + M4.D core: 31) stay green.

---

## Task 1: Foundation — Migration 19, ORM, Settings, Counter

**Files:**
- Create: `apps/api/migrations/versions/19_add_tenant_budget_credits.py`
- Modify: `apps/api/src/budget/models.py:1-92` (append `TenantBudgetCredit` after `TenantBudgetSnapshot`)
- Modify: `apps/api/src/core/config.py:147-156` (append 2 fields after `tenant_budget_*` block)
- Modify: `apps/api/src/core/business_metrics.py:90-101` (append after M4.D counters)
- Create: `apps/api/tests/budget/test_migration_19.py`

- [ ] **Step 1: Create migration `19_add_tenant_budget_credits.py`**

```python
"""add tenant_budget_credits — Pack B #2 top-up audit table

Per M4.D Pack B spec §3.1: super_admin grants are immutable append-only
audit records. Period-bound (UTC month) so per-period SUM is cheap.
FK to tenants.id with ON DELETE CASCADE.

Revision ID: 19_add_tenant_budget_credits
Revises: 18_add_soft_warn_fired_at
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "19_add_tenant_budget_credits"
down_revision = "18_add_soft_warn_fired_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_budget_credits",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period", sa.String(length=7), nullable=False),  # YYYY-MM
        sa.Column("tokens", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("granted_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("tokens > 0", name="ck_tenant_budget_credits_positive"),
    )
    op.create_index(
        "ix_tenant_budget_credits_tenant_period",
        "tenant_budget_credits",
        ["tenant_id", "period"],
    )


def downgrade() -> None:
    op.drop_index("ix_tenant_budget_credits_tenant_period", table_name="tenant_budget_credits")
    op.drop_table("tenant_budget_credits")
```

- [ ] **Step 2: Run migration upgrade to verify schema is valid**

```bash
cd apps/api && alembic upgrade head
```

Expected: `Running upgrade 18_add_soft_warn_fired_at -> 19_add_tenant_budget_credits`. No errors.

- [ ] **Step 3: Run migration downgrade to verify rollback works**

```bash
cd apps/api && alembic downgrade -1
```

Expected: `Running downgrade 19_add_tenant_budget_credits -> 18_add_soft_warn_fired_at`. No errors.

Then re-upgrade:

```bash
cd apps/api && alembic upgrade head
```

Expected: `Running upgrade 18_add_soft_warn_fired_at -> 19_add_tenant_budget_credits`.

- [ ] **Step 4: Add `TenantBudgetCredit` ORM to `apps/api/src/budget/models.py`**

Append after the `TenantBudgetSnapshot` class (line 89). Update the `__all__` list:

```python
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column


# Append after TenantBudgetSnapshot:


class TenantBudgetCredit(Base):
    """One row per super_admin credit grant — immutable audit log.

    Effective cap for a tenant in period P is computed as
    ``tenant_budgets.hard_cap_tokens + SUM(tenant_budget_credits.tokens
    WHERE period = P)``. Rows are append-only; no UPDATE/DELETE in app
    code. Negative-token revocations (future) would be new rows with
    negative tokens — schema accommodates but is out of Pack B scope.
    """

    __tablename__ = "tenant_budget_credits"
    __table_args__ = (
        Index(
            "ix_tenant_budget_credits_tenant_period",
            "tenant_id", "period",
        ),
        CheckConstraint("tokens > 0", name="ck_tenant_budget_credits_positive"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    granted_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


__all__ = ["TenantBudget", "TenantBudgetSnapshot", "TenantBudgetCredit"]
```

- [ ] **Step 5: Add 2 settings to `apps/api/src/core/config.py`**

Find the existing `tenant_budget_*` settings block and append after the last one. Read the file first to confirm exact insertion point:

```bash
grep -n "tenant_budget" apps/api/src/core/config.py
```

Then add (adjust line numbers based on grep output — typically append after the last `tenant_budget_cleanup_retention_months` line):

```python
    tenant_budget_429_skip_threshold_tokens: int = Field(
        default=1000,
        alias="TENANT_BUDGET_429_SKIP_THRESHOLD_TOKENS",
    )
    tenant_budget_per_model_cache_ttl_seconds: int = Field(
        default=30,
        alias="TENANT_BUDGET_PER_MODEL_CACHE_TTL_SECONDS",
    )
```

- [ ] **Step 6: Add `LLM_BUDGET_GATE_TOTAL` counter to `apps/api/src/core/business_metrics.py`**

Append after the existing `LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL` counter (Pack A). Read file first:

```bash
grep -n "LLM_BUDGET" apps/api/src/core/business_metrics.py
```

Then add:

```python
LLM_BUDGET_GATE_TOTAL = Counter(
    "lumen_llm_budget_gate_total",
    "Number of times BudgetResolver raised TenantBudgetRateLimited after observing 429",
)
```

- [ ] **Step 7: Write migration test**

```python
"""Pack B #2 — migration 19 creates tenant_budget_credits table."""
from __future__ import annotations

from sqlalchemy import inspect

from apps.api.src.core.database import get_engine   # adjust import to project convention


def test_upgrade_creates_tenant_budget_credits_table() -> None:
    """Upgrade 19 adds the table with all expected columns + index + check constraint."""
    # Run alembic upgrade head before this test (handled by conftest fixture)
    engine = get_engine()
    insp = inspect(engine)
    assert "tenant_budget_credits" in insp.get_table_names()
    cols = {c["name"]: c for c in insp.get_columns("tenant_budget_credits")}
    assert {"id", "tenant_id", "period", "tokens", "note", "granted_by", "created_at"} <= cols.keys()
    indexes = insp.get_indexes("tenant_budget_credits")
    assert any(
        ix["name"] == "ix_tenant_budget_credits_tenant_period"
        and set(ix["column_names"]) == {"tenant_id", "period"}
        for ix in indexes
    )
    checks = insp.get_check_constraints("tenant_budget_credits")
    assert any(c["name"] == "ck_tenant_budget_credits_positive" for c in checks)


def test_downgrade_drops_table() -> None:
    """Downgrade removes the table cleanly."""
    # Run alembic downgrade -1 (handled by conftest fixture)
    engine = get_engine()
    insp = inspect(engine)
    assert "tenant_budget_credits" not in insp.get_table_names()
```

**Adjust the import path** for `get_engine` to match the project's existing test convention — check `apps/api/tests/budget/conftest.py` or wherever migrations are exercised in Pack A's `test_migration_18.py`.

- [ ] **Step 8: Run test to verify it passes**

```bash
cd apps/api && pytest tests/budget/test_migration_19.py -v
```

Expected: 2 tests pass.

- [ ] **Step 9: Commit**

```bash
git add apps/api/migrations/versions/19_add_tenant_budget_credits.py \
        apps/api/src/budget/models.py \
        apps/api/src/core/config.py \
        apps/api/src/core/business_metrics.py \
        apps/api/tests/budget/test_migration_19.py
git commit -m "feat(budget): Pack B foundation — tenant_budget_credits table + 2 settings + gate counter"
```

---

## Task 2: CreditService + Admin Credit Endpoints

**Files:**
- Create: `apps/api/src/budget/credits.py`
- Modify: `apps/api/src/admin/schemas/budget.py`
- Modify: `apps/api/src/admin/api.py`
- Create: `apps/api/tests/budget/test_credits.py`
- Modify: `apps/api/tests/admin/test_budget_api.py`

- [ ] **Step 1: Write failing tests for `CreditService.grant`**

```python
"""Pack B #2 — CreditService.grant inserts immutable audit rows."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from apps.api.src.budget.credits import CreditService
from apps.api.src.budget.models import TenantBudgetCredit


def _make_session() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    s.flush = AsyncMock()
    return s


def _make_cache() -> MagicMock:
    c = MagicMock()
    c.invalidate = MagicMock()
    return c


async def test_grant_inserts_row_with_current_utc_period() -> None:
    """Spec §3.3: grant inserts a row with period=YYYY-MM (UTC) and ULID id."""
    session = _make_session()
    cache = _make_cache()
    svc = CreditService(session, cache)
    credit = await svc.grant(
        tenant_id="t1", tokens=500, note="Q4 promo", granted_by="super-uid"
    )
    assert isinstance(credit, TenantBudgetCredit)
    assert credit.tenant_id == "t1"
    assert credit.tokens == 500
    assert credit.note == "Q4 promo"
    assert credit.granted_by == "super-uid"
    assert credit.period == datetime.now(timezone.utc).strftime("%Y-%m")
    assert credit.id  # ULID is non-empty
    session.add.assert_called_once()
    session.flush.assert_awaited_once()
    cache.invalidate.assert_called_once_with("t1", period=credit.period)


async def test_grant_rejects_zero_tokens() -> None:
    svc = CreditService(_make_session(), _make_cache())
    with pytest.raises(ValueError, match="tokens must be > 0"):
        await svc.grant(tenant_id="t1", tokens=0, note="x", granted_by="u")


async def test_grant_rejects_negative_tokens() -> None:
    svc = CreditService(_make_session(), _make_cache())
    with pytest.raises(ValueError, match="tokens must be > 0"):
        await svc.grant(tenant_id="t1", tokens=-1, note="x", granted_by="u")


async def test_grant_rejects_empty_note() -> None:
    svc = CreditService(_make_session(), _make_cache())
    with pytest.raises(ValueError, match="note must be non-empty"):
        await svc.grant(tenant_id="t1", tokens=100, note="   ", granted_by="u")


async def test_grant_strips_note_whitespace() -> None:
    session = _make_session()
    cache = _make_cache()
    svc = CreditService(session, cache)
    credit = await svc.grant(
        tenant_id="t1", tokens=100, note="  padded note  ", granted_by="u"
    )
    assert credit.note == "padded note"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd apps/api && pytest tests/budget/test_credits.py -v
```

Expected: 5 tests FAIL with `ModuleNotFoundError: No module named 'apps.api.src.budget.credits'` (or similar — file doesn't exist yet).

- [ ] **Step 3: Implement `CreditService`**

```python
"""Credit grant service for M4.D Pack B #2.

Append-only audit log of super_admin credit grants. Effective cap is
computed downstream in BudgetResolver as
``hard_cap_tokens + sum_for_period(tenant_id, period)``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from budget.models import TenantBudgetCredit
from budget.resolver import _current_period

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from budget.per_model import PerModelBreakdownCache


def _new_ulid() -> str:
    """Return a fresh ULID. Uses project's ``core.id_gen.new_id`` helper,
    which is the same one Pack A's tests reference. Falls back to a
    direct ``ulid-py`` call only if the helper is unavailable.
    """
    from core.id_gen import new_id
    return new_id()


class CreditService:
    def __init__(self, session: "AsyncSession", per_model_cache: "PerModelBreakdownCache"):
        self._session = session
        self._per_model_cache = per_model_cache

    async def grant(
        self,
        *,
        tenant_id: str,
        tokens: int,
        note: str,
        granted_by: str,
    ) -> TenantBudgetCredit:
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        if not note.strip():
            raise ValueError("note must be non-empty")
        period, _ = _current_period("UTC")        # credits always bound to UTC month
        credit = TenantBudgetCredit(
            id=_new_ulid(),
            tenant_id=tenant_id,
            period=period,
            tokens=tokens,
            note=note.strip(),
            granted_by=granted_by,
        )
        self._session.add(credit)
        await self._session.flush()
        # Invalidate per-model cache so the next breakdown query reflects
        # the newly granted credit (effective_cap changed).
        self._per_model_cache.invalidate(tenant_id, period)
        return credit

    async def sum_for_period(self, tenant_id: str, period: str) -> int:
        result = await self._session.execute(
            select(func.coalesce(func.sum(TenantBudgetCredit.tokens), 0))
            .where(TenantBudgetCredit.tenant_id == tenant_id)
            .where(TenantBudgetCredit.period == period)
        )
        return int(result.scalar_one())

    async def list_for_period(self, tenant_id: str, period: str) -> list[TenantBudgetCredit]:
        result = await self._session.execute(
            select(TenantBudgetCredit)
            .where(TenantBudgetCredit.tenant_id == tenant_id)
            .where(TenantBudgetCredit.period == period)
            .order_by(TenantBudgetCredit.created_at.asc())
        )
        return list(result.scalars().all())


__all__ = ["CreditService"]
```

**Replace `_new_ulid()`** with the project's actual ULID helper. Check `apps/api/src/core/ids.py` (or similar) for the existing pattern used elsewhere in the codebase (e.g. Pack A's tests for ULID generation).

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd apps/api && pytest tests/budget/test_credits.py -v
```

Expected: 5 tests pass.

- [ ] **Step 5: Add `CreditRequest/CreditResponse/CreditListResponse` schemas**

Append to `apps/api/src/admin/schemas/budget.py`:

```python
from datetime import datetime


class CreditRequest(BaseModel):
    tokens: int = Field(..., gt=0)
    note: str = Field(..., min_length=1, max_length=500)


class CreditResponse(BaseModel):
    id: str
    tenant_id: str
    period: str
    tokens: int
    note: str
    granted_by: str
    created_at: datetime

    class Config:
        from_attributes = True


class CreditListResponse(BaseModel):
    credits: list[CreditResponse]
    total_tokens: int
```

Check the file's existing imports — adjust `BaseModel`, `Field` imports to match.

- [ ] **Step 6: Add admin endpoints to `apps/api/src/admin/api.py`**

Read the file first to find the existing budget endpoints and the helper for `require_admin`. Insert the new endpoints alongside them:

```bash
grep -n "budget\|require_admin" apps/api/src/admin/api.py | head -40
```

Then add (adjust imports as needed — `get_sessionmaker` already exists, `CreditService` is new):

```python
from fastapi import Query
from typing import Annotated, Any
from apps.api.src.auth.dependencies import require_admin
from apps.api.src.budget.credits import CreditService   # adjust import path
from apps.api.src.budget.per_model import PerModelBreakdownCache, get_per_model_cache  # Task 4
from apps.api.src.admin.schemas.budget import (
    CreditListResponse,
    CreditRequest,
    CreditResponse,
)
from apps.api.src.tenant.repository import TenantRepository   # existence check


@router.post(
    "/tenants/{tenant_id}/credits",
    status_code=201,
    response_model=CreditResponse,
)
async def grant_credit(
    tenant_id: str,
    body: CreditRequest,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> CreditResponse:
    # Super-admin only — mirrors Pack A's /budget/cleanup pattern.
    # Per-tenant admin tokens have tenant_id set; super-admin tokens have
    # tenant_id=None (via create_access_token's extra override).
    if claims.get("tenant_id") is not None:
        raise HTTPException(status_code=404, detail="not found")
    if await TenantRepository().get_by_id(tenant_id) is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    sm = get_sessionmaker()
    async with sm() as session:
        svc = CreditService(session, get_per_model_cache())
        credit = await svc.grant(
            tenant_id=tenant_id,
            tokens=body.tokens,
            note=body.note,
            granted_by=claims["sub"],
        )
        await session.commit()
    return CreditResponse.model_validate(credit)


@router.get(
    "/tenants/{tenant_id}/credits",
    response_model=CreditListResponse,
)
async def list_credits(
    tenant_id: str,
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    claims: Annotated[dict[str, Any], Depends(require_admin)] = None,
) -> CreditListResponse:
    # Super-admin only — same as POST above.
    if claims.get("tenant_id") is not None:
        raise HTTPException(status_code=404, detail="not found")
    if await TenantRepository().get_by_id(tenant_id) is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    sm = get_sessionmaker()
    async with sm() as session:
        svc = CreditService(session, get_per_model_cache())
        rows = await svc.list_for_period(tenant_id, period)
        total = await svc.sum_for_period(tenant_id, period)
    return CreditListResponse(
        credits=[CreditResponse.model_validate(r) for r in rows],
        total_tokens=total,
    )
```

**Adjust `_tenant_exists`** to match the project's actual helper (Pack A has one; check `apps/api/src/admin/api.py` or a sibling).

- [ ] **Step 7: Write failing API auth tests**

```python
"""Pack B #2 — admin credit endpoints require super_admin."""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_post_credits_as_super_admin_returns_201(
    async_client: AsyncClient, super_admin_headers: dict, seed_tenant: str
) -> None:
    resp = await async_client.post(
        f"/api/v1/admin/tenants/{seed_tenant}/credits",
        json={"tokens": 500, "note": "Q4 promo"},
        headers=super_admin_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["tenant_id"] == seed_tenant
    assert body["tokens"] == 500
    assert body["note"] == "Q4 promo"
    assert body["granted_by"]
    assert body["period"]


@pytest.mark.asyncio
async def test_post_credits_as_per_tenant_admin_returns_404(
    async_client: AsyncClient, per_tenant_admin_headers: dict, seed_tenant: str
) -> None:
    """Anti-enumeration: per-tenant admin probing → 404 (tenant_id != None)."""
    resp = await async_client.post(
        f"/api/v1/admin/tenants/{seed_tenant}/credits",
        json={"tokens": 500, "note": "probe"},
        headers=per_tenant_admin_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_credits_as_super_admin_returns_list(
    async_client: AsyncClient, super_admin_headers: dict, seed_tenant: str
) -> None:
    # Seed via direct DB or via the POST above; for unit-test isolation,
    # use a fixture that pre-inserts rows. Example below assumes fixture.
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{seed_tenant}/credits?period=2026-10",
        headers=super_admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "credits" in body
    assert "total_tokens" in body


@pytest.mark.asyncio
async def test_get_credits_as_per_tenant_admin_returns_404(
    async_client: AsyncClient, per_tenant_admin_headers: dict, seed_tenant: str
) -> None:
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{seed_tenant}/credits?period=2026-10",
        headers=per_tenant_admin_headers,
    )
    assert resp.status_code == 404
```

Add to `apps/api/tests/admin/test_budget_api.py` (the file already exists from Pack A — append the new tests, don't rewrite).

**Adjust fixtures** (`async_client`, `super_admin_headers`, `per_tenant_admin_headers`, `seed_tenant`) to match the project's existing conftest. Pack A's tests likely already have analogous fixtures.

- [ ] **Step 8: Run tests to verify they pass**

```bash
cd apps/api && pytest tests/admin/test_budget_api.py -v -k "credit"
```

Expected: 4 tests pass.

- [ ] **Step 9: Run full Pack A + new tests to confirm no regression**

```bash
cd apps/api && pytest tests/budget/ tests/admin/test_budget_api.py -v
```

Expected: all pass (49 existing + 5 new from this task).

- [ ] **Step 10: Commit**

```bash
git add apps/api/src/budget/credits.py \
        apps/api/src/admin/schemas/budget.py \
        apps/api/src/admin/api.py \
        apps/api/tests/budget/test_credits.py \
        apps/api/tests/admin/test_budget_api.py
git commit -m "feat(budget): Pack B #2 — CreditService + super_admin POST/GET /admin/tenants/{tid}/credits"
```

---

## Task 3: Resolver `_compute_effective_cap` Integration

**Files:**
- Modify: `apps/api/src/budget/resolver.py:1-273`
- Modify: `apps/api/tests/budget/test_resolver.py` (Pack A file — append tests)

- [ ] **Step 1: Write failing test for `_compute_effective_cap`**

Append to `apps/api/tests/budget/test_resolver.py`:

```python
"""Pack B #2 — resolver uses effective_cap = base + credits."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


async def test_pre_check_uses_effective_cap_includes_credits() -> None:
    """Spec §3.6: BudgetResolver treats effective_cap = base + sum_for_period(credits)."""
    # Setup: tenant with hard_cap=8000 base, 2000 credits for current period.
    # Effective cap = 10000. tokens_used = 8500 should pass pre-check
    # (8500 < 10000), even though 8500 > 8000 base.
    budget = _make_budget(hard_cap_tokens=8000)
    snap = _make_snapshot("t1", "2026-10", tokens_used=8500)
    cache = MagicMock()
    cache.get_or_load_async = AsyncMock(return_value=snap)
    snap_repo = MagicMock()
    snap_repo.get_for_tenant_period = AsyncMock(return_value=snap)
    snap_repo.upsert = AsyncMock()

    credit_svc = MagicMock()
    credit_svc.sum_for_period = AsyncMock(return_value=2000)

    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response(prompt=100, completion=50))

    from apps.api.src.budget.resolver import BudgetResolver
    resolver = BudgetResolver(
        inner=inner,
        tenant_id="t1",
        budget=budget,
        snapshot_cache=cache,
        snapshot_repo=snap_repo,
        credit_service=credit_svc,
    )
    await resolver.ainvoke(MagicMock(spec=ChatRequest))

    # Pre-check passed (no TenantBudgetExceeded raised).
    # Post-record was called.
    snap_repo.upsert.assert_awaited()
    # Credit sum was queried exactly once (lazy + scoped cache).
    credit_svc.sum_for_period.assert_awaited_once_with("t1", "2026-10")


async def test_pre_check_rejects_when_used_at_effective_cap() -> None:
    """tokens_used=10000 >= effective_cap=10000 → TenantBudgetExceeded."""
    from llm_client.exceptions import TenantBudgetExceeded

    budget = _make_budget(hard_cap_tokens=8000)
    snap = _make_snapshot("t1", "2026-10", tokens_used=10000)
    cache = MagicMock()
    cache.get_or_load_async = AsyncMock(return_value=snap)
    snap_repo = MagicMock()
    snap_repo.get_for_tenant_period = AsyncMock(return_value=snap)

    credit_svc = MagicMock()
    credit_svc.sum_for_period = AsyncMock(return_value=2000)

    inner = MagicMock()
    inner.ainvoke = AsyncMock()

    from apps.api.src.budget.resolver import BudgetResolver
    resolver = BudgetResolver(
        inner=inner,
        tenant_id="t1",
        budget=budget,
        snapshot_cache=cache,
        snapshot_repo=snap_repo,
        credit_service=credit_svc,
    )
    with pytest.raises(TenantBudgetExceeded):
        await resolver.ainvoke(MagicMock(spec=ChatRequest))
    inner.ainvoke.assert_not_called()


async def test_effective_cap_falls_back_when_no_credit_service() -> None:
    """Backward compat: credit_service=None → effective_cap = base."""
    from llm_client.exceptions import TenantBudgetExceeded

    budget = _make_budget(hard_cap_tokens=8000)
    snap = _make_snapshot("t1", "2026-10", tokens_used=8500)
    cache = MagicMock()
    cache.get_or_load_async = AsyncMock(return_value=snap)
    snap_repo = MagicMock()
    snap_repo.get_for_tenant_period = AsyncMock(return_value=snap)

    inner = MagicMock()
    inner.ainvoke = AsyncMock()

    from apps.api.src.budget.resolver import BudgetResolver
    resolver = BudgetResolver(
        inner=inner, tenant_id="t1", budget=budget,
        snapshot_cache=cache, snapshot_repo=snap_repo,
        # credit_service=None  → backward compat
    )
    with pytest.raises(TenantBudgetExceeded):
        await resolver.ainvoke(MagicMock(spec=ChatRequest))
```

Use the existing `_make_budget` / `_make_snapshot` / `_make_response` helpers from Pack A's `test_resolver.py`.

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd apps/api && pytest tests/budget/test_resolver.py -v -k "effective_cap"
```

Expected: 3 tests FAIL — `BudgetResolver.__init__` doesn't accept `credit_service` parameter.

- [ ] **Step 3: Modify `BudgetResolver` to accept `credit_service` and compute effective_cap**

In `apps/api/src/budget/resolver.py`:

Add to the imports at the top:

```python
from sqlalchemy.ext.asyncio import AsyncSession
```

Modify `__init__` to accept the new optional parameter. Find the existing constructor (around line 80):

```python
def __init__(
    self,
    *,
    inner,
    tenant_id: str,
    budget: TenantBudget,
    snapshot_cache,
    snapshot_repo,
    credit_service=None,        # NEW: Pack B #2
    per_model_cache=None,        # NEW: Pack B #5
):
    self._inner = inner
    self._tenant_id = tenant_id
    self._budget = budget
    self._snapshot_cache = snapshot_cache
    self._snapshot_repo = snapshot_repo
    self._credit_service = credit_service
    self._per_model_cache = per_model_cache
```

Add the helper method (after `_current_period` or near the other helpers):

```python
async def _compute_effective_cap(self, period: str) -> int:
    """Spec §3.6: effective cap = hard_cap_tokens + sum(credits for period)."""
    base = self._budget.hard_cap_tokens or 0
    if self._credit_service is None:
        return base
    return base + await self._credit_service.sum_for_period(self._tenant_id, period)
```

Find the existing `_pre_check` and `_post_record` methods (Pack A added them). Replace direct use of `self._budget.hard_cap_tokens` with `_compute_effective_cap(period)`. The pattern in Pack A looks like:

```python
# OLD (Pack A):
if snap.tokens_used >= self._budget.hard_cap_tokens:
    raise TenantBudgetExceeded(...)

# NEW (Pack B):
effective_cap = await self._compute_effective_cap(period)
if snap.tokens_used >= effective_cap:
    raise TenantBudgetExceeded(...)
```

Apply in both `_pre_check` and `_post_record`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd apps/api && pytest tests/budget/test_resolver.py -v
```

Expected: all tests pass (Pack A's 6 + new 3 = 9 total).

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/budget/resolver.py \
        apps/api/tests/budget/test_resolver.py
git commit -m "feat(budget): Pack B #2 — resolver computes effective_cap = base + credits"
```

---

## Task 4: PerModelBreakdownCache + PerModelService + Snapshot Extension

**Files:**
- Create: `apps/api/src/budget/per_model.py`
- Modify: `apps/api/src/admin/schemas/budget.py`
- Modify: `apps/api/src/admin/api.py`
- Modify: `apps/api/src/budget/resolver.py` (invalidate cache in `_post_record`)
- Create: `apps/api/tests/budget/test_per_model.py`
- Modify: `apps/api/tests/admin/test_budget_api.py`

- [ ] **Step 1: Write failing test for `PerModelBreakdownCache`**

```python
"""Pack B #5 — PerModelBreakdownCache hit/miss/TTL/invalidate."""
from __future__ import annotations

import time
from dataclasses import dataclass

from apps.api.src.budget.per_model import ModelUsage, PerModelBreakdownCache


def test_get_returns_none_on_miss() -> None:
    cache = PerModelBreakdownCache(ttl_seconds=30)
    assert cache.get("t1", "2026-10") is None


def test_set_then_get_returns_cached() -> None:
    cache = PerModelBreakdownCache(ttl_seconds=30)
    data = [ModelUsage(provider="openai", model="gpt-4o-mini",
                       prompt_tokens=100, completion_tokens=50,
                       total_tokens=150, request_count=1)]
    cache.set("t1", "2026-10", data)
    assert cache.get("t1", "2026-10") == data


def test_invalidate_drops_entry() -> None:
    cache = PerModelBreakdownCache(ttl_seconds=30)
    cache.set("t1", "2026-10", [])
    cache.invalidate("t1", "2026-10")
    assert cache.get("t1", "2026-10") is None


def test_ttl_expiry_evicts_entry() -> None:
    cache = PerModelBreakdownCache(ttl_seconds=1)
    cache.set("t1", "2026-10", [])
    time.sleep(1.1)
    assert cache.get("t1", "2026-10") is None


def test_different_tenants_isolated() -> None:
    cache = PerModelBreakdownCache(ttl_seconds=30)
    cache.set("t1", "2026-10", [ModelUsage("a", "b", 0, 0, 0, 0)])
    cache.invalidate("t1", "2026-10")
    assert cache.get("t2", "2026-10") is None


def test_different_periods_isolated() -> None:
    cache = PerModelBreakdownCache(ttl_seconds=30)
    cache.set("t1", "2026-10", [])
    cache.invalidate("t1", "2026-10")
    assert cache.get("t1", "2026-11") is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/api && pytest tests/budget/test_per_model.py -v -k "PerModelBreakdownCache"
```

Expected: FAIL — `ModuleNotFoundError: No module named 'apps.api.src.budget.per_model'`.

- [ ] **Step 3: Implement `PerModelBreakdownCache`**

```python
"""Per-model breakdown cache + service for M4.D Pack B #5.

Read-only observability: aggregates llm_usage GROUP BY (provider, model)
on demand, caches in-process for 30s. No cap enforcement. Cache
invalidation on each successful post_record and on credit grant.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from llm_client.models import LLMUsage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ModelUsage:
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    request_count: int


class PerModelBreakdownCache:
    """In-process dict cache keyed by (tenant_id, period)."""

    def __init__(self, ttl_seconds: int = 30):
        self._ttl = ttl_seconds
        self._store: dict[tuple[str, str], tuple[float, list[ModelUsage]]] = {}

    def get(self, tenant_id: str, period: str) -> list[ModelUsage] | None:
        entry = self._store.get((tenant_id, period))
        if entry is None:
            return None
        ts, data = entry
        if time.monotonic() - ts > self._ttl:
            self._store.pop((tenant_id, period), None)
            return None
        return data

    def set(self, tenant_id: str, period: str, data: list[ModelUsage]) -> None:
        self._store[(tenant_id, period)] = (time.monotonic(), data)

    def invalidate(self, tenant_id: str, period: str) -> None:
        self._store.pop((tenant_id, period), None)


class PerModelService:
    """Read-only per-model breakdown with TTL cache."""

    def __init__(self, session: "AsyncSession", cache: PerModelBreakdownCache):
        self._session = session
        self._cache = cache

    async def get_breakdown(self, tenant_id: str, period: str) -> list[ModelUsage]:
        cached = self._cache.get(tenant_id, period)
        if cached is not None:
            return cached
        period_start = datetime.strptime(period + "-01", "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
        result = await self._session.execute(
            select(
                LLMUsage.provider,
                LLMUsage.model,
                func.coalesce(func.sum(LLMUsage.prompt_tokens), 0),
                func.coalesce(func.sum(LLMUsage.completion_tokens), 0),
                func.count(),
            )
            .where(LLMUsage.tenant_id == tenant_id)
            .where(LLMUsage.created_at >= period_start)
            .where(LLMUsage.cached == False)  # noqa: E712 — only billable calls
            .group_by(LLMUsage.provider, LLMUsage.model)
        )
        rows = [
            ModelUsage(
                provider=row.provider,
                model=row.model,
                prompt_tokens=int(row[2] or 0),
                completion_tokens=int(row[3] or 0),
                total_tokens=int(row[2] or 0) + int(row[3] or 0),
                request_count=int(row[4]),
            )
            for row in result
        ]
        self._cache.set(tenant_id, period, rows)
        return rows


# Module-level singleton for the FastAPI app
_default_cache: PerModelBreakdownCache | None = None


def get_per_model_cache(ttl_seconds: int = 30) -> PerModelBreakdownCache:
    """Spec §4.2: process-local default cache. Reused across requests."""
    global _default_cache
    if _default_cache is None:
        _default_cache = PerModelBreakdownCache(ttl_seconds=ttl_seconds)
    return _default_cache


__all__ = [
    "ModelUsage",
    "PerModelBreakdownCache",
    "PerModelService",
    "get_per_model_cache",
]
```

**Verify** that `LLMUsage.cached`, `LLMUsage.provider`, `LLMUsage.model` fields exist (Pack A already uses them). Check `apps/api/src/llm_client/models.py`.

- [ ] **Step 4: Run cache tests to verify they pass**

```bash
cd apps/api && pytest tests/budget/test_per_model.py -v -k "PerModelBreakdownCache"
```

Expected: 6 tests pass.

- [ ] **Step 5: Write failing test for `PerModelService` (DB integration)**

```python
"""Pack B #5 — PerModelService queries llm_usage with cache."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from apps.api.src.budget.per_model import (
    ModelUsage,
    PerModelBreakdownCache,
    PerModelService,
)


async def _seed_usage(session, *, tenant_id, provider, model, prompt, completion, created_at):
    """Insert a billable LLMUsage row. Adjust to project's LLMUsage fixture helper."""
    from llm_client.models import LLMUsage
    row = LLMUsage(
        id=str(__import__("ulid").ULID()),
        tenant_id=tenant_id, provider=provider, model=model,
        prompt_tokens=prompt, completion_tokens=completion,
        cost_usd=0.0, request_id="r", cached=False,
        metadata_json={}, created_at=created_at,
    )
    session.add(row)
    await session.flush()


@pytest.mark.asyncio
async def test_get_breakdown_groups_by_provider_model(db_session) -> None:
    """GROUP BY (provider, model) sums prompt/completion tokens."""
    period_start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    await _seed_usage(db_session, tenant_id="t1", provider="openai", model="gpt-4o-mini",
                      prompt=100, completion=50, created_at=period_start)
    await _seed_usage(db_session, tenant_id="t1", provider="openai", model="gpt-4o-mini",
                      prompt=200, completion=100, created_at=period_start)
    await _seed_usage(db_session, tenant_id="t1", provider="anthropic", model="haiku",
                      prompt=50, completion=25, created_at=period_start)
    cache = PerModelBreakdownCache(ttl_seconds=30)
    svc = PerModelService(db_session, cache)
    rows = await svc.get_breakdown("t1", "2026-10")
    assert {r.provider for r in rows} == {"openai", "anthropic"}
    openai = next(r for r in rows if r.provider == "openai")
    assert openai.model == "gpt-4o-mini"
    assert openai.prompt_tokens == 300
    assert openai.completion_tokens == 150
    assert openai.request_count == 2


@pytest.mark.asyncio
async def test_get_breakdown_caches_within_ttl(db_session) -> None:
    """Second call within TTL returns cached; no second DB hit."""
    period_start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    await _seed_usage(db_session, tenant_id="t1", provider="openai", model="gpt-4o",
                      prompt=10, completion=5, created_at=period_start)
    cache = PerModelBreakdownCache(ttl_seconds=30)
    svc = PerModelService(db_session, cache)
    await svc.get_breakdown("t1", "2026-10")
    # Add another row — should NOT be visible (cached).
    await _seed_usage(db_session, tenant_id="t1", provider="openai", model="gpt-4o",
                      prompt=999, completion=999, created_at=period_start)
    rows2 = await svc.get_breakdown("t1", "2026-10")
    assert rows2[0].prompt_tokens == 10   # cached, not refreshed
    # Invalidate → next call sees fresh data.
    cache.invalidate("t1", "2026-10")
    rows3 = await svc.get_breakdown("t1", "2026-10")
    assert rows3[0].prompt_tokens == 1009


@pytest.mark.asyncio
async def test_get_breakdown_excludes_cached_rows(db_session) -> None:
    """LLMUsage.cached=True rows are excluded (cache hits aren't billable)."""
    period_start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    from llm_client.models import LLMUsage
    row = LLMUsage(
        id=str(__import__("ulid").ULID()),
        tenant_id="t1", provider="openai", model="gpt-4o-mini",
        prompt_tokens=100, completion_tokens=50,
        cost_usd=0.0, request_id="r", cached=True,    # cached!
        metadata_json={}, created_at=period_start,
    )
    db_session.add(row)
    await db_session.flush()
    cache = PerModelBreakdownCache(ttl_seconds=30)
    svc = PerModelService(db_session, cache)
    rows = await svc.get_breakdown("t1", "2026-10")
    assert rows == []
```

**Adjust `db_session` fixture** to match the project's existing conftest pattern. Pack A uses one — look at `apps/api/tests/budget/conftest.py`.

- [ ] **Step 6: Run integration tests to verify they pass**

```bash
cd apps/api && pytest tests/budget/test_per_model.py -v -k "PerModelService or breakdown"
```

Expected: 3 tests pass.

- [ ] **Step 7: Add `BreakdownItem` schema + extend `TenantBudgetUsageRead`**

The actual Pack A schema for the snapshot endpoint is `TenantBudgetUsageRead` (not `BudgetSnapshotResponse`). In `apps/api/src/admin/schemas/budget.py`, extend it:

```python
class BreakdownItem(BaseModel):
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    request_count: int


# Existing class (Pack A) — extend in place:
class TenantBudgetUsageRead(BaseModel):
    """Response body — live usage snapshot for the current period.

    Pack B additions: `effective_cap` (base + credits), `credits_total`
    (sum of credits for current period), `breakdown` (per-model array,
    populated only when ?breakdown=true).
    """
    period: str
    tokens_used: int
    soft_warn_tokens: int | None
    hard_cap_tokens: int | None
    period_starts_at: datetime
    effective_cap: int                                # NEW (Pack B #2)
    credits_total: int                                # NEW (Pack B #2)
    breakdown: list[BreakdownItem] | None = None      # NEW (Pack B #5)
```

And update `__all__`:

```python
__all__ = [
    "BreakdownItem",
    "CleanupResponse",
    "TenantBudgetCreate",
    "TenantBudgetRead",
    "TenantBudgetUsageRead",
]
```

- [ ] **Step 8: Extend snapshot endpoint in `apps/api/src/admin/api.py`**

Find the existing `GET /tenants/{tenant_id}/budget/usage` endpoint (Pack A) and modify it. The actual path is `/api/v1/admin/tenants/{tenant_id}/budget/usage` (router prefix `/api/v1/admin`). Auth stays the same: `Depends(require_admin)` + `claims.get("tenant_id") != tenant_id → 404`.

Replace the existing `get_tenant_budget_usage` function with:

```python
@router.get(
    "/tenants/{tenant_id}/budget/usage",
    response_model=TenantBudgetUsageRead,
)
async def get_tenant_budget_usage(
    tenant_id: str,
    breakdown: bool = Query(default=False, description="Include per-model breakdown"),
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> TenantBudgetUsageRead:
    """Read the current period's token usage snapshot for a tenant.

    Pack B adds: `effective_cap` (base + credits), `credits_total` (sum of
    credits for current period), and optional `breakdown` (per-provider/
    per-model token sums) when `?breakdown=true`.
    """
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    try:
        budget = await AdminTenantBudgetRepository().get(tenant_id=tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    period, period_start = _current_period(
        budget.period_anchor_tz if budget else "UTC"
    )
    sm = get_sessionmaker()
    async with sm() as session:
        snap = await TenantBudgetSnapshotRepository().refresh(
            tenant_id=tenant_id, period=period, period_starts_at=period_start,
        )
        # NEW (Pack B #2): credits for current period.
        credit_svc = CreditService(session, get_per_model_cache())
        credits_total = await credit_svc.sum_for_period(tenant_id, period)
        # NEW (Pack B #5): optional per-model breakdown.
        breakdown_items: list[BreakdownItem] | None = None
        if breakdown:
            pm_svc = PerModelService(session, get_per_model_cache())
            breakdown_items = [
                BreakdownItem(
                    provider=r.provider, model=r.model,
                    prompt_tokens=r.prompt_tokens,
                    completion_tokens=r.completion_tokens,
                    total_tokens=r.total_tokens,
                    request_count=r.request_count,
                )
                for r in await pm_svc.get_breakdown(tenant_id, period)
            ]
    base_cap = budget.hard_cap_tokens if budget else None
    effective_cap = (base_cap or 0) + credits_total
    return TenantBudgetUsageRead(
        period=snap.period,
        tokens_used=snap.tokens_used,
        soft_warn_tokens=budget.soft_warn_tokens if budget else None,
        hard_cap_tokens=base_cap,
        effective_cap=effective_cap,                       # NEW
        credits_total=credits_total,                       # NEW
        breakdown=breakdown_items,                         # NEW (None unless ?breakdown=true)
        period_starts_at=period_start,
    )
```

**Note:** The existing `TenantBudgetUsageRead` schema is extended (Step 7) to include the new fields. Auth unchanged. Backward compat: without `?breakdown=true`, `breakdown=None` in response; existing fields unchanged. With `?breakdown=true`, per-model array is included.

- [ ] **Step 9: Wire `_post_record` to invalidate per-model cache**

In `apps/api/src/budget/resolver.py`, find `_post_record` and add at the end:

```python
async def _post_record(self, *, resp, period: str):
    # ... existing Pack A logic ...
    self._snapshot_repo.set_tokens_used(...)
    # NEW: Pack B #5 — invalidate per-model cache so next breakdown reflects this write.
    if self._per_model_cache is not None:
        self._per_model_cache.invalidate(self._tenant_id, period)
```

- [ ] **Step 10: Write API test for breakdown endpoint**

```python
@pytest.mark.asyncio
async def test_get_snapshot_with_breakdown_true_includes_breakdown(
    async_client: AsyncClient, seed_tenant_with_usage: str, admin_token_for
) -> None:
    """?breakdown=true returns per-model array in snapshot response."""
    token = admin_token_for(tenant_id=seed_tenant_with_usage)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{seed_tenant_with_usage}/budget/usage?breakdown=true",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "breakdown" in body
    assert isinstance(body["breakdown"], list)
    assert "effective_cap" in body
    assert "credits_total" in body


@pytest.mark.asyncio
async def test_get_snapshot_without_breakdown_omits_breakdown(
    async_client: AsyncClient, seed_tenant: str, admin_token_for
) -> None:
    """Without ?breakdown=true, breakdown field is null (Pack A backward compat)."""
    token = admin_token_for(tenant_id=seed_tenant)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{seed_tenant}/budget/usage",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("breakdown") is None
    # Existing Pack A fields still present.
    assert "tokens_used" in body
    assert "soft_warn_tokens" in body
    assert "hard_cap_tokens" in body


@pytest.mark.asyncio
async def test_get_snapshot_cross_tenant_returns_404(
    async_client: AsyncClient, admin_token_for
) -> None:
    """Cross-tenant admin probing → 404 (existing Pack A anti-enumeration)."""
    other_tenant_token = admin_token_for(tenant_id="t-other")
    resp = await async_client.get(
        "/api/v1/admin/tenants/t-target/budget/usage",
        headers=auth_headers(other_tenant_token),
    )
    assert resp.status_code == 404
```

Add to `apps/api/tests/admin/test_budget_api.py`.

- [ ] **Step 11: Run all new tests**

```bash
cd apps/api && pytest tests/budget/test_per_model.py tests/admin/test_budget_api.py -v
```

Expected: all pass.

- [ ] **Step 12: Commit**

```bash
git add apps/api/src/budget/per_model.py \
        apps/api/src/admin/schemas/budget.py \
        apps/api/src/admin/api.py \
        apps/api/src/budget/resolver.py \
        apps/api/tests/budget/test_per_model.py \
        apps/api/tests/admin/test_budget_api.py
git commit -m "feat(budget): Pack B #5 — PerModelBreakdownCache + PerModelService + ?breakdown=true endpoint"
```

---

## Task 5: TenantBudgetRateLimited Exception + Resolver Gate

**Files:**
- Create: `apps/api/src/budget/exceptions.py`
- Modify: `apps/api/src/budget/resolver.py`
- Modify: `apps/api/src/main.py`
- Create: `apps/api/tests/budget/test_budget_gate.py`

- [ ] **Step 1: Write failing test for `TenantBudgetRateLimited` exception**

```python
"""Pack B #8 — TenantBudgetRateLimited exception + gate logic."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from apps.api.src.budget.exceptions import TenantBudgetRateLimited


def test_exception_carries_full_context() -> None:
    """Spec §5.1: tenant_id, period, remaining, threshold all on the exception."""
    exc = TenantBudgetRateLimited(
        tenant_id="t1", period="2026-10", remaining=50, threshold=1000
    )
    assert exc.tenant_id == "t1"
    assert exc.period == "2026-10"
    assert exc.remaining == 50
    assert exc.threshold == 1000
    assert "t1" in str(exc)
    assert "2026-10" in str(exc)
    assert "50" in str(exc)
    assert "1000" in str(exc)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/api && pytest tests/budget/test_budget_gate.py::test_exception_carries_full_context -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'apps.api.src.budget.exceptions'`.

- [ ] **Step 3: Implement `TenantBudgetRateLimited`**

```python
"""Pack B #8 — budget-side rate-limit exception.

Raised when BudgetResolver observes a 429 from the inner provider
chain AND remaining budget is below a configurable threshold. Signals
outer code: tenant has hit a budget wall while providers are still
rate-limiting — do not retry, surface as HTTP 429.
"""
from __future__ import annotations


class TenantBudgetRateLimited(Exception):
    def __init__(
        self,
        *,
        tenant_id: str,
        period: str,
        remaining: int,
        threshold: int,
    ):
        self.tenant_id = tenant_id
        self.period = period
        self.remaining = remaining
        self.threshold = threshold
        super().__init__(
            f"tenant {tenant_id} budget low in {period}: "
            f"remaining={remaining} < threshold={threshold} after 429"
        )


__all__ = ["TenantBudgetRateLimited"]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd apps/api && pytest tests/budget/test_budget_gate.py::test_exception_carries_full_context -v
```

Expected: PASS.

- [ ] **Step 5: Write failing test for the gate firing**

```python
"""Pack B #8 — BudgetResolver fires gate when remaining < threshold AND 429 observed."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from apps.api.src.budget.exceptions import TenantBudgetRateLimited
from apps.api.src.budget.resolver import BudgetResolver
from llm_client.exceptions import RateLimited


def _make_response(prompt=100, completion=50):
    from llm_client.types import ChatResponse
    return ChatResponse(
        content="ok", provider="openai", model="gpt-4o-mini",
        prompt_tokens=prompt, completion_tokens=completion,
        cost_usd=0.0, request_id="r", cached=False,
    )


def _make_budget(*, hard_cap=8000, soft_warn=5000):
    from apps.api.src.budget.models import TenantBudget
    return TenantBudget(
        id="b1", tenant_id="t1",
        hard_cap_tokens=hard_cap, soft_warn_tokens=soft_warn,
        period_anchor_tz="UTC",
    )


def _make_snapshot(tenant_id, period, *, tokens_used=0, soft_warn_fired_at=None):
    from apps.api.src.budget.models import TenantBudgetSnapshot
    from datetime import datetime, timezone
    return TenantBudgetSnapshot(
        id="s1", tenant_id=tenant_id, period=period,
        tokens_used=tokens_used,
        last_refreshed_at=datetime.now(timezone.utc),
        soft_warn_fired_at=soft_warn_fired_at,
    )


async def test_gate_raises_when_remaining_below_threshold() -> None:
    """Spec §5.2: snap at effective_cap-500 (< 1000 threshold) + 429 → gate fires."""
    budget = _make_budget(hard_cap=8000)
    snap = _make_snapshot("t1", "2026-10", tokens_used=7500)  # effective_cap=8000, remaining=500
    cache = MagicMock()
    cache.get_or_load_async = AsyncMock(return_value=snap)
    snap_repo = MagicMock()
    snap_repo.get_for_tenant_period = AsyncMock(return_value=snap)
    credit_svc = MagicMock()
    credit_svc.sum_for_period = AsyncMock(return_value=0)
    per_model_cache = MagicMock()
    per_model_cache.invalidate = MagicMock()

    inner = MagicMock()
    inner.ainvoke = AsyncMock(side_effect=RateLimited("429 from openai"))

    resolver = BudgetResolver(
        inner=inner, tenant_id="t1", budget=budget,
        snapshot_cache=cache, snapshot_repo=snap_repo,
        credit_service=credit_svc, per_model_cache=per_model_cache,
    )

    with pytest.raises(TenantBudgetRateLimited) as exc_info:
        await resolver.ainvoke(MagicMock(spec=__import__("llm_client").ChatRequest if False else object))
    assert exc_info.value.remaining == 500
    assert exc_info.value.threshold == 1000
    # Per-model cache was invalidated (the 429 still hit a provider).
    per_model_cache.invalidate.assert_called_with("t1", "2026-10")
    # No post_record (no tokens billed).
    snap_repo.set_tokens_used.assert_not_called()


async def test_gate_does_not_raise_when_remaining_above_threshold() -> None:
    """remaining=5000 > 1000 → 429 propagates as-is."""
    budget = _make_budget(hard_cap=8000)
    snap = _make_snapshot("t1", "2026-10", tokens_used=3000)
    cache = MagicMock()
    cache.get_or_load_async = AsyncMock(return_value=snap)
    snap_repo = MagicMock()
    snap_repo.get_for_tenant_period = AsyncMock(return_value=snap)
    credit_svc = MagicMock()
    credit_svc.sum_for_period = AsyncMock(return_value=0)
    per_model_cache = MagicMock()

    inner = MagicMock()
    inner.ainvoke = AsyncMock(side_effect=RateLimited("429"))

    resolver = BudgetResolver(
        inner=inner, tenant_id="t1", budget=budget,
        snapshot_cache=cache, snapshot_repo=snap_repo,
        credit_service=credit_svc, per_model_cache=per_model_cache,
    )

    with pytest.raises(RateLimited):
        await resolver.ainvoke(MagicMock())


async def test_gate_disabled_when_threshold_zero() -> None:
    """TENANT_BUDGET_429_SKIP_THRESHOLD_TOKENS=0 disables the gate."""
    from core.config import get_settings
    settings = get_settings()
    original = settings.tenant_budget_429_skip_threshold_tokens
    settings.tenant_budget_429_skip_threshold_tokens = 0
    try:
        budget = _make_budget(hard_cap=8000)
        snap = _make_snapshot("t1", "2026-10", tokens_used=7999)  # remaining=1
        cache = MagicMock()
        cache.get_or_load_async = AsyncMock(return_value=snap)
        snap_repo = MagicMock()
        snap_repo.get_for_tenant_period = AsyncMock(return_value=snap)
        credit_svc = MagicMock()
        credit_svc.sum_for_period = AsyncMock(return_value=0)

        inner = MagicMock()
        inner.ainvoke = AsyncMock(side_effect=RateLimited("429"))

        resolver = BudgetResolver(
            inner=inner, tenant_id="t1", budget=budget,
            snapshot_cache=cache, snapshot_repo=snap_repo,
            credit_service=credit_svc,
        )
        with pytest.raises(RateLimited):     # NOT TenantBudgetRateLimited
            await resolver.ainvoke(MagicMock())
    finally:
        settings.tenant_budget_429_skip_threshold_tokens = original


async def test_gate_uses_effective_cap_not_base() -> None:
    """When credits are present, gate uses effective_cap = base + credits."""
    budget = _make_budget(hard_cap=8000)
    snap = _make_snapshot("t1", "2026-10", tokens_used=8500)
    cache = MagicMock()
    cache.get_or_load_async = AsyncMock(return_value=snap)
    snap_repo = MagicMock()
    snap_repo.get_for_tenant_period = AsyncMock(return_value=snap)
    credit_svc = MagicMock()
    credit_svc.sum_for_period = AsyncMock(return_value=2000)  # +2000 credits
    # effective_cap = 8000 + 2000 = 10000
    # remaining = 10000 - 8500 = 1500 > 1000 → gate does NOT fire.
    per_model_cache = MagicMock()

    inner = MagicMock()
    inner.ainvoke = AsyncMock(side_effect=RateLimited("429"))

    resolver = BudgetResolver(
        inner=inner, tenant_id="t1", budget=budget,
        snapshot_cache=cache, snapshot_repo=snap_repo,
        credit_service=credit_svc, per_model_cache=per_model_cache,
    )
    with pytest.raises(RateLimited):    # NOT TenantBudgetRateLimited
        await resolver.ainvoke(MagicMock())


async def test_gate_metric_increments() -> None:
    """LLM_BUDGET_GATE_TOTAL += 1 on gate fire."""
    from core.business_metrics import LLM_BUDGET_GATE_TOTAL
    before = LLM_BUDGET_GATE_TOTAL._value.get()

    budget = _make_budget(hard_cap=8000)
    snap = _make_snapshot("t1", "2026-10", tokens_used=7500)
    cache = MagicMock()
    cache.get_or_load_async = AsyncMock(return_value=snap)
    snap_repo = MagicMock()
    snap_repo.get_for_tenant_period = AsyncMock(return_value=snap)
    credit_svc = MagicMock()
    credit_svc.sum_for_period = AsyncMock(return_value=0)
    per_model_cache = MagicMock()

    inner = MagicMock()
    inner.ainvoke = AsyncMock(side_effect=RateLimited("429"))

    resolver = BudgetResolver(
        inner=inner, tenant_id="t1", budget=budget,
        snapshot_cache=cache, snapshot_repo=snap_repo,
        credit_service=credit_svc, per_model_cache=per_model_cache,
    )
    with pytest.raises(TenantBudgetRateLimited):
        await resolver.ainvoke(MagicMock())

    after = LLM_BUDGET_GATE_TOTAL._value.get()
    assert after == before + 1
```

- [ ] **Step 6: Run tests to verify they fail**

```bash
cd apps/api && pytest tests/budget/test_budget_gate.py -v
```

Expected: gate tests FAIL — gate logic doesn't exist yet.

- [ ] **Step 7: Modify `BudgetResolver.ainvoke` to add gate logic**

In `apps/api/src/budget/resolver.py`, add imports:

```python
from llm_client.exceptions import RateLimited
from budget.exceptions import TenantBudgetRateLimited
from core.business_metrics import LLM_BUDGET_GATE_TOTAL
from core.config import get_settings
```

Modify `ainvoke` to wrap the inner call in try/except `RateLimited`:

```python
async def ainvoke(self, request):
    settings = get_settings()
    period, _ = _current_period(self._budget.period_anchor_tz)
    snap, _ = await self._ensure_snapshot(period)
    effective_cap = await self._compute_effective_cap(period)
    if snap.tokens_used >= effective_cap:
        raise TenantBudgetExceeded(...)  # existing Pack A path

    try:
        resp = await self._inner.ainvoke(request)
    except RateLimited:
        # Provider chain exhausted on 429. Invalidate per-model cache
        # (a request did hit a provider), do NOT post_record (no tokens
        # billed), then check budget gate.
        if self._per_model_cache is not None:
            self._per_model_cache.invalidate(self._tenant_id, period)
        await self._maybe_raise_budget_gate(period, settings)
        raise   # gate did not fire → propagate 429

    await self._post_record(resp=resp, period=period)
    if self._per_model_cache is not None:
        self._per_model_cache.invalidate(self._tenant_id, period)
    return resp


async def _maybe_raise_budget_gate(self, period: str, settings) -> None:
    """Spec §5.2: post-mortem gate. Fires when remaining < threshold."""
    snap, _ = await self._ensure_snapshot(period)
    effective_cap = await self._compute_effective_cap(period)
    remaining = effective_cap - snap.tokens_used
    threshold = settings.tenant_budget_429_skip_threshold_tokens
    if remaining < threshold:
        LLM_BUDGET_GATE_TOTAL.inc()
        raise TenantBudgetRateLimited(
            tenant_id=self._tenant_id,
            period=period,
            remaining=remaining,
            threshold=threshold,
        )
```

- [ ] **Step 8: Run tests to verify they pass**

```bash
cd apps/api && pytest tests/budget/test_budget_gate.py -v
```

Expected: 5 tests pass (gate raises, gate doesn't raise, gate disabled, gate uses effective cap, metric increments).

- [ ] **Step 9: Register FastAPI exception handler in `apps/api/src/main.py`**

Add near the top (after imports):

```python
from budget.exceptions import TenantBudgetRateLimited
from fastapi.responses import JSONResponse


@app.exception_handler(TenantBudgetRateLimited)
async def _budget_rate_limited_handler(request, exc):
    return JSONResponse(
        status_code=429,
        content={
            "error": "budget_rate_limited",
            "tenant_id": exc.tenant_id,
            "period": exc.period,
            "remaining": exc.remaining,
            "threshold": exc.threshold,
        },
    )
```

- [ ] **Step 10: Write handler test**

```python
@pytest.mark.asyncio
async def test_handler_returns_429_with_structured_body(
    async_client: AsyncClient, seed_tenant: str, super_admin_headers: dict
) -> None:
    """FastAPI returns HTTP 429 with error=budget_rate_limited JSON."""
    # Trigger gate via direct call to a stub endpoint, or mock the resolver.
    # Simpler: raise the exception in a synthetic route registered in test.
    from apps.api.src.budget.exceptions import TenantBudgetRateLimited
    from main import app

    @app.get("/_test/raise_gate")
    async def _raise():
        raise TenantBudgetRateLimited(
            tenant_id="t1", period="2026-10", remaining=50, threshold=1000
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/_test/raise_gate", headers=super_admin_headers)
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"] == "budget_rate_limited"
    assert body["tenant_id"] == "t1"
    assert body["remaining"] == 50
    assert body["threshold"] == 1000
```

The test registers a synthetic endpoint that raises the exception — this exercises the handler without needing a full LLM request flow.

- [ ] **Step 11: Run all budget tests**

```bash
cd apps/api && pytest tests/budget/ -v
```

Expected: all pass (Pack A's 18 + Pack B Task 3's 3 + Task 4's 9 + this task's 6 = 36 budget tests, plus handler test).

- [ ] **Step 12: Commit**

```bash
git add apps/api/src/budget/exceptions.py \
        apps/api/src/budget/resolver.py \
        apps/api/src/main.py \
        apps/api/tests/budget/test_budget_gate.py
git commit -m "feat(budget): Pack B #8 — TenantBudgetRateLimited exception + post-mortem gate + handler + metric"
```

---

## Task 6: E2E + README Close-out

**Files:**
- Create: `apps/api/tests/budget/integration/test_pack_b_e2e.py`
- Modify: `README.md`
- Create: `~/.claude/projects/.../memory/m4-d-pack-b-progress.md`
- Modify: `~/.claude/projects/.../memory/MEMORY.md`

- [ ] **Step 1: Write the e2e test**

```python
"""Pack B end-to-end: credit grant → effective cap → breakdown → gate."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from apps.api.src.budget.exceptions import TenantBudgetRateLimited
from apps.api.src.budget.resolver import BudgetResolver
from core.business_metrics import LLM_BUDGET_GATE_TOTAL
from llm_client.exceptions import RateLimited


def _response(prompt=100, completion=50):
    from llm_client.types import ChatResponse
    return ChatResponse(
        content="ok", provider="openai", model="gpt-4o-mini",
        prompt_tokens=prompt, completion_tokens=completion,
        cost_usd=0.0, request_id="r", cached=False,
    )


async def test_e2e_credit_grant_then_breakdown_then_gate(db_session) -> None:
    """Spec §6: full Pack B happy path + gate trigger."""
    # Setup: tenant with hard_cap=1000, no credits, soft_warn=2000.
    from apps.api.src.budget.models import (
        TenantBudget,
        TenantBudgetSnapshot,
    )
    from apps.api.src.budget.credits import CreditService
    from apps.api.src.budget.per_model import (
        PerModelBreakdownCache,
        PerModelService,
    )
    from ulid import ULID

    tenant_id = "tenant-e2e-packb"
    period = datetime.now(timezone.utc).strftime("%Y-%m")

    budget = TenantBudget(
        id=str(ULID()), tenant_id=tenant_id,
        hard_cap_tokens=1000, soft_warn_tokens=2000,
        period_anchor_tz="UTC",
    )
    snap = TenantBudgetSnapshot(
        id=str(ULID()), tenant_id=tenant_id, period=period,
        tokens_used=0, last_refreshed_at=datetime.now(timezone.utc),
    )
    db_session.add(budget)
    db_session.add(snap)

    # Seed two distinct (provider, model) llm_usage rows.
    from llm_client.models import LLMUsage
    for prov, model, p, c in [
        ("openai", "gpt-4o-mini", 700, 350),
        ("anthropic", "haiku", 500, 250),
    ]:
        db_session.add(LLMUsage(
            id=str(ULID()), tenant_id=tenant_id, provider=prov, model=model,
            prompt_tokens=p, completion_tokens=c,
            cost_usd=0.0, request_id="r", cached=False,
            metadata_json={}, created_at=datetime.now(timezone.utc),
        ))
    await db_session.flush()

    # 1. Super_admin grants 500 tokens → effective_cap becomes 1500.
    cache = PerModelBreakdownCache(ttl_seconds=30)
    credit_svc = CreditService(db_session, cache)
    await credit_svc.grant(
        tenant_id=tenant_id, tokens=500, note="e2e", granted_by="super"
    )

    # 2. Per-model breakdown returns 2 entries summing to 1450.
    pm_svc = PerModelService(db_session, cache)
    rows = await pm_svc.get_breakdown(tenant_id, period)
    assert len(rows) == 2
    assert sum(r.total_tokens for r in rows) == 1800   # 700+350+500+250

    # 3. Resolver with snap at 1450 + 429 → gate fires (remaining=50 < 1000).
    snap_repo = MagicMock()
    snap_repo.get_for_tenant_period = AsyncMock(return_value=snap)
    snap_repo.upsert = AsyncMock()
    snap_repo.set_tokens_used = AsyncMock()
    snap_cache = MagicMock()
    snap_cache.get_or_load_async = AsyncMock(return_value=snap)
    snap_cache.invalidate = MagicMock()
    before_metric = LLM_BUDGET_GATE_TOTAL._value.get()

    inner = MagicMock()
    inner.ainvoke = AsyncMock(side_effect=RateLimited("429"))

    resolver = BudgetResolver(
        inner=inner, tenant_id=tenant_id, budget=budget,
        snapshot_cache=snap_cache, snapshot_repo=snap_repo,
        credit_service=credit_svc, per_model_cache=cache,
    )
    with pytest.raises(TenantBudgetRateLimited) as exc:
        await resolver.ainvoke(MagicMock())
    assert exc.value.remaining == 50
    assert LLM_BUDGET_GATE_TOTAL._value.get() == before_metric + 1

    # 4. No post_record (no tokens billed).
    snap_repo.set_tokens_used.assert_not_called()
```

- [ ] **Step 2: Run e2e test to verify it passes**

```bash
cd apps/api && pytest tests/budget/integration/test_pack_b_e2e.py -v
```

Expected: PASS.

- [ ] **Step 3: Run ALL M4.D tests to confirm no regression**

```bash
cd apps/api && pytest tests/budget/ tests/admin/test_budget_api.py -v
```

Expected: all pass (49 M4.D core + 18 Pack A + 27 Pack B = 94 tests).

- [ ] **Step 4: Update README — remove closed tech debt items**

Find the M4.D tech debt section:

```bash
grep -n "No top-up mechanism\|No per-model breakdown\|No provider 429" README.md
```

Replace the 3 closed items with strikethrough + Pack B references (matches Pack A's pattern):

```markdown
2. **No top-up mechanism** — ~~Deferred to Pack B.~~ **Closed in Pack B.**
5. **No per-model breakdown** — ~~Deferred to Pack B.~~ **Closed in Pack B.**
8. **No provider 429 integration** — ~~Deferred to Pack B.~~ **Closed in Pack B.**
```

Keep #6 deferred to Pack C unchanged.

- [ ] **Step 5: Add known-limitation note**

In the same tech debt section, add at the end:

```markdown
**Known limitations (Pack B):**
- **Post-mortem gate observes after chain exhausted** — does not preempt mid-chain retries. If a tenant's budget can only afford 1 retry but the chain tries 3, the gate fires after all 3 fail. Pre-flight rejection would require deeper M4.B FallbackResolver coupling.
```

- [ ] **Step 6: Commit README + e2e**

```bash
git add apps/api/tests/budget/integration/test_pack_b_e2e.py README.md
git commit -m "docs + test: Pack B close-out — e2e + README tech debt update"
```

- [ ] **Step 7: Write memory file**

Write to `~/.claude/projects/D--work-ai-0401-ai-customer/memory/m4-d-pack-b-progress.md`:

```markdown
---
name: m4-d-pack-b-progress
description: M4.D Pack B shipped (#2 + #5 + #8 closed)
metadata:
  type: project
---

M4.D Pack B shipped to origin/main. Closes 3 of 4 deferred M4.D tech debt items:

- **#2 Top-up mechanism** — `tenant_budget_credits` table (immutable audit log), super_admin POST/GET endpoints, effective_cap = base + credits. UTC-month-bound.
- **#5 Per-model breakdown** — `PerModelBreakdownCache` (in-process 30s TTL), `PerModelService` lazy GROUP BY on `llm_usage`, `?breakdown=true` query param on snapshot endpoint. Read-only observability, no cap enforcement.
- **#8 Provider 429 budget gate** — `TenantBudgetRateLimited` exception + post-mortem check at `BudgetResolver.ainvoke` (after `FallbackResolver` exhausts chain). Configurable threshold via `TENANT_BUDGET_429_SKIP_THRESHOLD_TOKENS=1000`. New `LLM_BUDGET_GATE_TOTAL` metric + FastAPI handler → HTTP 429.

**#6** (non-LLM cost budget) remains deferred to Pack C.

**Why:** Pack B = single PR + single release. Implementation order: #2 → #5 → #8 (risk递增, gate observes full effective_cap semantics).

**How to apply:** Reference this when implementing Pack C or any future budget feature. The post-mortem gate has a known limitation (doesn't preempt mid-chain retries) — pre-flight would require deeper M4.B coupling. Per-model cache is in-process (not Redis) — fine for single API process, would need rethinking for multi-replica.

Related: [[m4-d-pack-a-progress]], [[m4-d-progress]]
```

- [ ] **Step 8: Update MEMORY.md index**

Add a one-line pointer to `~/.claude/projects/D--work-ai-0401-ai-customer/memory/MEMORY.md`:

```markdown
- [M4.D Pack B progress](m4-d-pack-b-progress.md) — M4.D Pack B shipped (#2 + #5 + #8 closed, 27 new tests, single PR)
```

- [ ] **Step 9: Final commit**

```bash
git status   # verify nothing leftover
git log --oneline -8   # verify 6 Pack B commits
```

Expected: 6 commits on top of Pack A's tip:
1. `feat(budget): Pack B foundation — tenant_budget_credits table + 2 settings + gate counter`
2. `feat(budget): Pack B #2 — CreditService + super_admin POST/GET /admin/budget/{tid}/credits`
3. `feat(budget): Pack B #2 — resolver computes effective_cap = base + credits`
4. `feat(budget): Pack B #5 — PerModelBreakdownCache + PerModelService + ?breakdown=true endpoint`
5. `feat(budget): Pack B #8 — TenantBudgetRateLimited exception + post-mortem gate + handler + metric`
6. `docs + test: Pack B close-out — e2e + README tech debt update`

Then push:
```bash
git push origin main
```
