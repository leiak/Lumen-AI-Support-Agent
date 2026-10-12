"""add tenant_fallback_chains table

Revision ID: 21_tenant_fallback_chains
Revises: 20_add_widget_configs
Create Date: 2026-10-12

Per M5.1 spec: per-tenant ordered list of LLM (provider, model) pairs
to try in sequence when a tenant's primary call fails. One row per
tenant. ``steps`` is a JSONB list of ``{provider, model, enabled}``
dicts; a GIN index supports later analytics queries over the chain.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "21_tenant_fallback_chains"
down_revision = "20_add_widget_configs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_fallback_chains",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(length=26),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "steps",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", name="uq_tenant_fallback_chains_tenant_id"),
    )
    op.create_index(
        "ix_tenant_fallback_chains_tenant_id",
        "tenant_fallback_chains",
        ["tenant_id"],
    )
    op.create_index(
        "ix_tenant_fallback_chains_steps_gin",
        "tenant_fallback_chains",
        ["steps"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tenant_fallback_chains_steps_gin",
        table_name="tenant_fallback_chains",
    )
    op.drop_index(
        "ix_tenant_fallback_chains_tenant_id",
        table_name="tenant_fallback_chains",
    )
    op.drop_table("tenant_fallback_chains")