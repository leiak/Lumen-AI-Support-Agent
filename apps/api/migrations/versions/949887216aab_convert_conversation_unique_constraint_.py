"""convert conversation unique constraint to partial index on non-closed rows

Revision ID: 949887216aab
Revises: 2a30fc3b9f9e
Create Date: 2026-09-10 16:00:02.107005

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '949887216aab'
down_revision: str | Sequence[str] | None = '2a30fc3b9f9e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Replace the (channel_id, customer_external_id) UNIQUE constraint with a
    partial UNIQUE index that only applies when status != 'closed'. This
    allows history of closed conversations to coexist while still
    preventing two OPEN/PENDING conversations for the same customer on
    the same channel.
    """
    op.drop_constraint(
        "uq_conversations_channel_customer",
        "conversations",
        type_="unique",
    )
    op.create_index(
        "uq_conversations_channel_customer_open",
        "conversations",
        ["channel_id", "customer_external_id"],
        unique=True,
        postgresql_where=sa.text("status != 'closed'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "uq_conversations_channel_customer_open",
        table_name="conversations",
    )
    op.create_unique_constraint(
        "uq_conversations_channel_customer",
        "conversations",
        ["channel_id", "customer_external_id"],
    )
