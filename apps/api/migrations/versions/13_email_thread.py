"""Add email_thread_id to conversations

Revision ID: 13_email_thread
Revises: 12_ticket_tables
Create Date: 2026-09-19
"""
from alembic import op
import sqlalchemy as sa

revision = "13_email_thread"
down_revision = "12_ticket_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("email_thread_id", sa.String(128), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("email_message_id_header", sa.String(128), nullable=True),
    )
    op.create_index(
        "idx_conversations_email_thread",
        "conversations",
        ["tenant_id", "email_thread_id"],
    )
    # Idempotency: SES retries on 5xx, so dedupe by message_id
    op.create_unique_constraint(
        "uq_conversations_email_message_id",
        "conversations",
        ["email_message_id_header"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_conversations_email_message_id", "conversations")
    op.drop_index("idx_conversations_email_thread", table_name="conversations")
    op.drop_column("conversations", "email_message_id_header")
    op.drop_column("conversations", "email_thread_id")
