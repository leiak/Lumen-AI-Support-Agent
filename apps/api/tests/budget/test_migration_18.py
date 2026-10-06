"""Migration tests for 18_add_soft_warn_fired_at (M4.D Pack A).

Verifies alembic upgrade adds the column and downgrade drops it.
"""
from __future__ import annotations

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from core.database import get_engine


def _columns_for(table_name: str) -> dict[str, dict]:
    """Return a ``{column_name: column_dict}`` mapping for ``table_name``.

    Bridges the AsyncEngine → Inspector gap by running ``inspect()`` in
    a greenlet context via ``conn.run_sync``. SQLAlchemy 2.0 rejects
    ``inspect(async_engine)`` outright and ``sync_engine`` (with the
    asyncpg dialect) cannot be awaited from a sync context, so the only
    portable path is to open an async connection and run the sync
    inspection through it.
    """
    engine = get_engine()

    async def _run() -> dict[str, dict]:
        async with engine.connect() as conn:
            return await conn.run_sync(
                lambda sync_conn: {
                    col["name"]: col
                    for col in inspect(sync_conn).get_columns(table_name)
                }
            )

    return asyncio.run(_run())


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
    cols = _columns_for("tenant_budget_snapshots")
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
    cols = _columns_for("tenant_budget_snapshots")
    assert "soft_warn_fired_at" not in cols
    # Re-upgrade to leave DB clean for the next test.
    command.upgrade(cfg, "head")
