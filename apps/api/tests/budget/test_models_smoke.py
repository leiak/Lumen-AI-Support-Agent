"""Smoke tests for budget ORM models — import + column existence.

Heavy CRUD behavior is covered by test_repository.py (Task 2) against
the real Postgres testcontainer. These two smoke tests verify the
schema is wired correctly and can be imported without errors.
"""
from __future__ import annotations

import pytest

from budget.models import TenantBudget, TenantBudgetSnapshot
from core.database import Base, get_engine


def test_models_register_with_base() -> None:
    """Both ORM classes register on the metadata so alembic can find them."""
    assert TenantBudget.__tablename__ == "tenant_budgets"
    assert TenantBudgetSnapshot.__tablename__ == "tenant_budget_snapshots"
    # Both registered on the shared metadata
    assert "tenant_budgets" in Base.metadata.tables
    assert "tenant_budget_snapshots" in Base.metadata.tables


@pytest.mark.skip_postgres
async def test_tables_have_expected_columns() -> None:
    """Post-migration column check (skipped without a live PG)."""
    from sqlalchemy import inspect

    engine = get_engine()
    async with engine.connect() as conn:
        cols = await conn.run_sync(
            lambda sync: {c["name"] for c in inspect(sync).get_columns("tenant_budgets")}
        )
    expected = {"id", "tenant_id", "soft_warn_tokens", "hard_cap_tokens",
                "period_anchor_tz", "updated_at"}
    assert expected.issubset(cols)
