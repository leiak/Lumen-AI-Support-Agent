"""ticket tables

Revision ID: 12_ticket_tables
Revises: 16008dafeff0
Create Date: 2026-09-19 09:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '12_ticket_tables'
down_revision: str | Sequence[str] | None = '16008dafeff0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ticket domain tables.

    Two Postgres ENUMs (``ticket_priority``, ``ticket_status``) and three
    tenant-scoped tables (``sla_policies``, ``tickets``,
    ``ticket_events``), plus a nullable ``ticket_id`` FK on
    ``conversations``.

    Table creation order:
        1. ``sla_policies`` (no incoming FKs)
        2. ``tickets`` (FK to ``sla_policies``, ``conversations``,
           ``users``)
        3. ``ticket_events`` (FK to ``tickets``)
        4. ``conversations.ticket_id`` (FK back to ``tickets``)

    Indexes are created after each table. Partial indexes use
    ``postgresql_where`` to keep them small.

    The Postgres ENUMs are created up-front; the column declarations
    use ``create_type=False`` so SQLAlchemy doesn't try to CREATE TYPE
    again during autogenerate / ``metadata.create_all()``.
    """
    ticket_priority = postgresql.ENUM("P0", "P1", "P2", "P3", name="ticket_priority")
    ticket_status = postgresql.ENUM(
        "new",
        "triaged",
        "in_progress",
        "waiting_customer",
        "resolved",
        "closed",
        "cancelled",
        name="ticket_status",
    )
    ticket_priority.create(op.get_bind())
    ticket_status.create(op.get_bind())

    # --- sla_policies (created BEFORE tickets because tickets has FK) ---
    op.create_table(
        "sla_policies",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("tenant_id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "priority",
            postgresql.ENUM(name="ticket_priority", create_type=False),
            nullable=False,
        ),
        sa.Column("first_response_minutes", sa.Integer(), nullable=False),
        sa.Column("resolution_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "business_hours_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_sla_policies_tenant_name"),
    )

    # --- tickets ---
    op.create_table(
        "tickets",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("tenant_id", sa.String(length=26), nullable=False),
        sa.Column("conversation_id", sa.String(length=26), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column(
            "priority",
            postgresql.ENUM(name="ticket_priority", create_type=False),
            nullable=False,
            server_default=sa.text("'P2'"),
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="ticket_status", create_type=False),
            nullable=False,
            server_default=sa.text("'new'"),
        ),
        sa.Column("assignee_agent_id", sa.String(length=26), nullable=True),
        sa.Column("sla_policy_id", sa.String(length=26), nullable=True),
        sa.Column("sla_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["assignee_agent_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["sla_policy_id"], ["sla_policies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", name="uq_tickets_conversation_id"),
    )
    op.create_index(
        "idx_tickets_tenant_status",
        "tickets",
        ["tenant_id", "status"],
        unique=False,
    )
    op.create_index(
        "idx_tickets_assignee",
        "tickets",
        ["tenant_id", "assignee_agent_id"],
        unique=False,
        postgresql_where=sa.text("assignee_agent_id IS NOT NULL"),
    )
    op.create_index(
        "idx_tickets_sla",
        "tickets",
        ["sla_deadline_at"],
        unique=False,
        postgresql_where=sa.text(
            "status NOT IN ('resolved', 'closed', 'cancelled')"
        ),
    )

    # --- ticket_events ---
    op.create_table(
        "ticket_events",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("tenant_id", sa.String(length=26), nullable=False),
        sa.Column("ticket_id", sa.String(length=26), nullable=False),
        sa.Column("actor_type", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.String(length=26), nullable=True),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_ticket_events_ticket",
        "ticket_events",
        ["tenant_id", "ticket_id", "created_at"],
        unique=False,
    )

    # --- conversations.ticket_id (back-pointer) ---
    op.add_column(
        "conversations",
        sa.Column("ticket_id", sa.String(length=26), nullable=True),
    )
    op.create_foreign_key(
        "fk_conversations_ticket_id",
        "conversations",
        "tickets",
        ["ticket_id"],
        ["id"],
    )


def downgrade() -> None:
    """Reverse the ticket domain tables in reverse FK order.

    Order:
        1. Drop FK on ``conversations.ticket_id`` and the column itself.
        2. Drop ``ticket_events`` (depends on ``tickets``).
        3. Drop ``tickets`` indexes, then ``tickets`` table (the FK
           ``tickets.conversation_id`` is dropped implicitly with the
           table).
        4. Drop ``sla_policies`` (no incoming FKs by this point).
        5. Drop the Postgres ENUM types.
    """
    op.drop_constraint(
        "fk_conversations_ticket_id", "conversations", type_="foreignkey"
    )
    op.drop_column("conversations", "ticket_id")

    op.drop_index("idx_ticket_events_ticket", table_name="ticket_events")
    op.drop_table("ticket_events")

    op.drop_index("idx_tickets_sla", table_name="tickets")
    op.drop_index("idx_tickets_assignee", table_name="tickets")
    op.drop_index("idx_tickets_tenant_status", table_name="tickets")
    op.drop_table("tickets")

    op.drop_table("sla_policies")

    op.execute("DROP TYPE ticket_status")
    op.execute("DROP TYPE ticket_priority")