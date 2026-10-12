"""add tenant_llm_config_audit_log table (M5.3 audit log)

Revision ID: 23_tenant_llm_config_audit_log
Revises: 22_tenant_llm_configs_model
Create Date: 2026-10-12 19:00:00.000000

Append-only audit log of every field change made via the admin REST
surface to ``tenant_llm_configs``. Closes README tech debt line 372.

Design notes (from spec §5):

* One row per field change (fine-grained). PATCH that touches 2
  fields produces 2 rows.
* ``field`` is a VARCHAR(32) NOT NULL — the value space is enforced
  by Pydantic ``Literal[...]`` at the ORM boundary, NOT a DB enum
  (so adding a new field value is a code change, not a migration).
* No FK to ``tenant_llm_configs``: a DELETE on the source row does
  NOT cascade to audit; the audit row remains as the historical
  record. CASCADE only fires on tenant deletion.
* Indexes serve two read paths: the GET endpoint
  (newest-first per tenant) and the optional provider filter
  (per-provider history).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "23_tenant_llm_config_audit_log"
down_revision = "22_tenant_llm_configs_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_llm_config_audit_log",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(length=26),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("field", sa.String(length=32), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.String(length=64), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_audit_log_tenant_changed_at",
        "tenant_llm_config_audit_log",
        ["tenant_id", sa.text("changed_at DESC")],
    )
    op.create_index(
        "ix_audit_log_tenant_provider_changed_at",
        "tenant_llm_config_audit_log",
        ["tenant_id", "provider_name", sa.text("changed_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audit_log_tenant_provider_changed_at",
        table_name="tenant_llm_config_audit_log",
    )
    op.drop_index(
        "ix_audit_log_tenant_changed_at",
        table_name="tenant_llm_config_audit_log",
    )
    op.drop_table("tenant_llm_config_audit_log")