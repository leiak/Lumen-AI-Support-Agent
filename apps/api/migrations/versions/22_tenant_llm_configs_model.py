"""add model column to tenant_llm_configs (M5.2 per-tenant model override)

Revision ID: 22_tenant_llm_configs_model
Revises: 21_tenant_fallback_chains
Create Date: 2026-10-12 18:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "22_tenant_llm_configs_model"
down_revision = "21_tenant_fallback_chains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable ``model`` column to ``tenant_llm_configs``.

    NULL = no per-tenant override (back-compat; project default applies).
    Non-NULL = override; the per-tenant resolver wrapper rewrites
    ``request.model`` to this value before the provider receives the
    request (M5.2).

    No new index needed because the existing
    ``ix_tenant_llm_configs_tenant_id`` covers the (tenant_id, provider_name)
    lookup pattern (model lookups are joined on this key).
    """
    op.add_column(
        "tenant_llm_configs",
        sa.Column("model", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant_llm_configs", "model")