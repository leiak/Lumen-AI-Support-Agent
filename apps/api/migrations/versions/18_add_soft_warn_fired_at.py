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
