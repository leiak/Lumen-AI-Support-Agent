"""Add kb_multimodal_articles table

Stage 17 / M2.B Task 6 — multimodal KB foundation.

Carries the metadata + storage URL + chunk counts for image/PDF articles
indexed through ``POST /api/v1/kb-articles/multimodal``. Actual vectors
live in Qdrant (``kb_image_vectors`` collection — see
:func:`knowledge.startup.ensure_image_collection`).

Revision ID: 14_kb_multimodal
Revises: 13_email_thread
Create Date: 2026-09-20
"""
from alembic import op
import sqlalchemy as sa

revision = "14_kb_multimodal"
down_revision = "13_email_thread"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kb_multimodal_articles",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("tenant_id", sa.String(26), nullable=False, index=True),
        sa.Column("kb_slug", sa.String(64), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("file_url", sa.Text, nullable=False),
        sa.Column("mime_type", sa.String(64), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger, nullable=False),
        sa.Column("text_chunks_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("image_chunks_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_kb_mm_articles_tenant_kb",
        "kb_multimodal_articles",
        ["tenant_id", "kb_slug", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_kb_mm_articles_tenant_kb", table_name="kb_multimodal_articles")
    op.drop_table("kb_multimodal_articles")
