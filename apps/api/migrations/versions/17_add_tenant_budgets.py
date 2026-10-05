"""add tenant_budgets + tenant_budget_snapshots tables

Revision ID: 17_add_tenant_budgets
Revises: 16_add_tenant_llm_configs
Create Date: 2026-10-05

Per M4.D spec §5.1:
- tenant_budgets: per-tenant cap config (NULL = unlimited / no warn)
- tenant_budget_snapshots: cached SUM(llm_usage) per (tenant, period)
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "17_add_tenant_budgets"
down_revision = "16_add_tenant_llm_configs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_budgets",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(length=26),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("soft_warn_tokens", sa.BigInteger(), nullable=True),
        sa.Column("hard_cap_tokens", sa.BigInteger(), nullable=True),
        sa.Column("period_anchor_tz", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )

    op.create_table(
        "tenant_budget_snapshots",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(length=26),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period", sa.String(length=7), nullable=False),  # YYYY-MM
        sa.Column("tokens_used", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "last_refreshed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", "period", name="uq_tenant_budget_snapshots_tenant_period"),
    )
    op.create_index(
        "ix_tenant_budget_snapshots_tenant_id",
        "tenant_budget_snapshots",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_tenant_budget_snapshots_tenant_id", table_name="tenant_budget_snapshots")
    op.drop_table("tenant_budget_snapshots")
    op.drop_table("tenant_budgets")
