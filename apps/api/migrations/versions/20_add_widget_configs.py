"""add widget_configs — Tier 2 Task 2.4

Per-tenant widget appearance config (theme, primary_color, position,
welcome_message, etc.). One row per tenant (tenant_id is the PK).
FK to tenants.id with ON DELETE CASCADE.

Revision ID: 20_add_widget_configs
Revises: 19_add_tenant_budget_credits
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20_add_widget_configs"
down_revision = "19_add_tenant_budget_credits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "widget_configs",
        sa.Column(
            "tenant_id",
            sa.String(length=26),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("theme", sa.String(length=10), nullable=False, server_default="light"),
        sa.Column(
            "primary_color", sa.String(length=7), nullable=False, server_default="#2563eb"
        ),
        sa.Column(
            "position", sa.String(length=20), nullable=False, server_default="bottom-right"
        ),
        sa.Column(
            "widget_title", sa.String(length=64), nullable=False, server_default="在线客服"
        ),
        sa.Column(
            "welcome_message",
            sa.Text(),
            nullable=False,
            server_default="您好!请描述您的问题,我们会尽快为您解答。",
        ),
        sa.Column("logo_url", sa.String(length=500), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "theme IN ('light', 'dark', 'auto')", name="ck_widget_configs_theme"
        ),
        sa.CheckConstraint(
            "position IN ('bottom-right', 'bottom-left')",
            name="ck_widget_configs_position",
        ),
        sa.CheckConstraint(
            "primary_color ~ '^#[0-9A-Fa-f]{6}$'",
            name="ck_widget_configs_primary_color_hex",
        ),
    )


def downgrade() -> None:
    op.drop_table("widget_configs")