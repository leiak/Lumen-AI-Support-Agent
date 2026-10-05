"""add tenant_llm_configs table

Revision ID: 16_add_tenant_llm_configs
Revises: 15_kb_drafts
Create Date: 2026-10-05

Per M4.C spec §6.1: one row per (tenant_id, provider_name), holds
Fernet-encrypted API key + optional base_url override + enabled flag.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "16_add_tenant_llm_configs"
down_revision = "15_kb_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_llm_configs",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(length=26),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("encrypted_api_key", sa.LargeBinary(), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
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
        sa.UniqueConstraint("tenant_id", "provider_name", name="uq_tenant_llm_configs_tenant_provider"),
    )
    op.create_index(
        "ix_tenant_llm_configs_tenant_id",
        "tenant_llm_configs",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_tenant_llm_configs_tenant_id", table_name="tenant_llm_configs")
    op.drop_table("tenant_llm_configs")
