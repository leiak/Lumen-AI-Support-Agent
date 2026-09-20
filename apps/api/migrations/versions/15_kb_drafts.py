"""Add kb_article_drafts table

Stage 18 / M2.B Task 8 — KB draft review pipeline. The
``history_mining_worker`` (Sunday cron) generates
``KbArticleDraft`` rows from HDBSCAN-clustered customer questions;
admins approve / reject them via ``/api/v1/admin/kb-drafts/{id}/{approve,reject}``.

``source_questions`` is a JSON list of message IDs whose text formed
the cluster — kept as opaque IDs so the admin endpoint never echoes
raw customer text back in an unauthenticated listing body.

Revision ID: 15_kb_drafts
Revises: 14_kb_multimodal
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "15_kb_drafts"
down_revision = "14_kb_multimodal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kb_article_drafts",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("tenant_id", sa.String(26), nullable=False, index=True),
        sa.Column("cluster_id", sa.Integer, nullable=False),
        sa.Column("source_questions", sa.JSON, nullable=False),
        sa.Column("suggested_title", sa.Text, nullable=False),
        sa.Column("suggested_body", sa.Text, nullable=False),
        sa.Column(
            "suggested_tags", sa.JSON, nullable=False, server_default="[]"
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("published_article_id", sa.String(26), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(26), nullable=True),
    )
    op.create_index(
        "idx_kb_drafts_tenant_status",
        "kb_article_drafts",
        ["tenant_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_kb_drafts_tenant_status", table_name="kb_article_drafts")
    op.drop_table("kb_article_drafts")