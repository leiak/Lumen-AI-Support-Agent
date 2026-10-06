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
