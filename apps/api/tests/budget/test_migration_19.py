"""Migration tests for 19_add_tenant_budget_credits (M4.D Pack B).

Verifies alembic upgrade creates the table with the expected columns,
index, and CHECK constraint; downgrade drops the table.

Note: this file mirrors the pattern in :mod:`tests.budget.test_migration_18`
— all inspection queries run inside a single ``asyncio.run()`` to avoid
the "Event loop is closed" issue when the AsyncEngine is reused across
multiple top-level event loops within one test.
"""
from __future__ import annotations

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from core.database import get_engine


def _inspect_schema(table_name: str) -> dict:
    """Single async hop: collect table existence, columns, indexes, and checks."""
    engine = get_engine()

    async def _run() -> dict:
        async with engine.connect() as conn:
            def _collect(sync_conn: object) -> dict:
                insp = inspect(sync_conn)
                return {
                    "exists": table_name in insp.get_table_names(),
                    "columns": {
                        c["name"]: c
                        for c in insp.get_columns(table_name)
                    } if table_name in insp.get_table_names() else {},
                    "indexes": list(insp.get_indexes(table_name))
                    if table_name in insp.get_table_names() else [],
                    "checks": list(insp.get_check_constraints(table_name))
                    if table_name in insp.get_table_names() else [],
                }

            return await conn.run_sync(_collect)

    return asyncio.run(_run())


@pytest.fixture
def _alembic_upgraded_to_19() -> None:
    """Apply all migrations up to the head revision (includes migration 19)."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


def test_upgrade_creates_tenant_budget_credits_table(_alembic_upgraded_to_19: None) -> None:
    """After upgrade to head, tenant_budget_credits table exists with all expected
    columns, the (tenant_id, period) index, and the tokens > 0 check constraint."""
    schema = _inspect_schema("tenant_budget_credits")
    assert schema["exists"], "tenant_budget_credits table should exist after upgrade"
    cols = schema["columns"]
    expected = {"id", "tenant_id", "period", "tokens", "note", "granted_by", "created_at"}
    assert expected <= cols.keys(), (
        f"missing columns; expected {expected}, got {set(cols.keys())}"
    )

    indexes = schema["indexes"]
    assert any(
        ix["name"] == "ix_tenant_budget_credits_tenant_period"
        and set(ix["column_names"]) == {"tenant_id", "period"}
        for ix in indexes
    ), f"missing expected index; got {indexes!r}"

    checks = schema["checks"]
    assert any(c["name"] == "ck_tenant_budget_credits_positive" for c in checks), (
        f"missing tokens > 0 check constraint; got {checks!r}"
    )


def test_downgrade_drops_tenant_budget_credits_table() -> None:
    """After downgrade -1 from head, the table is removed."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")  # back to 18_add_soft_warn_fired_at
    schema = _inspect_schema("tenant_budget_credits")
    assert not schema["exists"], "tenant_budget_credits table should be gone after downgrade"
    # Re-upgrade to leave DB clean for the next test.
    command.upgrade(cfg, "head")
