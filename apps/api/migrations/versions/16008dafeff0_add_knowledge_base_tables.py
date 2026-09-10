"""add knowledge base tables

Revision ID: 16008dafeff0
Revises: 949887216aab
Create Date: 2026-09-10 17:54:27.448821

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '16008dafeff0'
down_revision: str | Sequence[str] | None = '949887216aab'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the knowledge base RAG tables.

    Four new tables:
      * ``knowledge_bases`` — per-tenant named collection of articles,
        with its own embedding model + chunking parameters.
      * ``articles`` — source document with lifecycle status.
      * ``article_versions`` — immutable snapshot of an article's raw
        text; re-indexes create new versions.
      * ``chunks`` — retrieval unit. Tenant + KB + article are
        denormalized onto each chunk for cheap tenant-scoped RAG queries.

    All FKs cascade on delete so a single tenant delete cleans up the
    whole subtree (matches the conversation/channel convention).
    """
    # knowledge_bases
    op.create_table(
        'knowledge_bases',
        sa.Column('id', sa.String(length=26), nullable=False),
        sa.Column('tenant_id', sa.String(length=26), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('slug', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('embedding_model', sa.String(length=100), nullable=False),
        sa.Column('chunk_size', sa.Integer(), nullable=False),
        sa.Column('chunk_overlap', sa.Integer(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'slug', name='uq_knowledge_bases_tenant_slug'),
    )

    # articles
    op.create_table(
        'articles',
        sa.Column('id', sa.String(length=26), nullable=False),
        sa.Column('tenant_id', sa.String(length=26), nullable=False),
        sa.Column('knowledge_base_id', sa.String(length=26), nullable=False),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.Column('source_uri', sa.String(length=2000), nullable=True),
        sa.Column('source_type', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('current_version_id', sa.String(length=26), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['knowledge_base_id'], ['knowledge_bases.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_articles_tenant_kb_status',
        'articles',
        ['tenant_id', 'knowledge_base_id', 'status'],
        unique=False,
    )
    op.create_index(
        'ix_articles_kb_status',
        'articles',
        ['knowledge_base_id', 'status'],
        unique=False,
    )

    # article_versions
    op.create_table(
        'article_versions',
        sa.Column('id', sa.String(length=26), nullable=False),
        sa.Column('article_id', sa.String(length=26), nullable=False),
        sa.Column('version_number', sa.Integer(), nullable=False),
        sa.Column('raw_text', sa.Text(), nullable=False),
        sa.Column('content_hash', sa.Text(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['article_id'], ['articles.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'article_id', 'version_number', name='uq_article_versions_article_version'
        ),
    )
    op.create_index(
        'ix_article_versions_article_content_hash',
        'article_versions',
        ['article_id', 'content_hash'],
        unique=False,
    )
    # FK from articles.current_version_id -> article_versions.id.
    # Must be created AFTER article_versions exists. Nullable + ON DELETE
    # SET NULL so deleting a version NULLs the article's pointer instead
    # of leaving a dangling reference. Skipped if already present
    # (idempotent on re-run / DBs that predate this hand-edit).
    bind = op.get_bind()
    existing = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_constraint "
            "WHERE conname = 'fk_articles_current_version_id'"
        )
    ).first()
    if existing is None:
        op.create_foreign_key(
            'fk_articles_current_version_id',
            'articles',
            'article_versions',
            ['current_version_id'],
            ['id'],
            ondelete='SET NULL',
        )

    # chunks
    op.create_table(
        'chunks',
        sa.Column('id', sa.String(length=26), nullable=False),
        sa.Column('article_version_id', sa.String(length=26), nullable=False),
        sa.Column('tenant_id', sa.String(length=26), nullable=False),
        sa.Column('knowledge_base_id', sa.String(length=26), nullable=False),
        sa.Column('article_id', sa.String(length=26), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('token_count', sa.Integer(), nullable=True),
        sa.Column(
            'metadata_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column('qdrant_point_id', sa.String(length=100), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['article_version_id'], ['article_versions.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(['article_id'], ['articles.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['knowledge_base_id'], ['knowledge_bases.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'article_version_id', 'chunk_index', name='uq_chunks_version_index'
        ),
    )
    op.create_index(
        'ix_chunks_tenant_kb',
        'chunks',
        ['tenant_id', 'knowledge_base_id'],
        unique=False,
    )


def downgrade() -> None:
    """Drop the knowledge base tables in reverse FK order.

    Order: chunks -> article_versions -> articles -> knowledge_bases.
    The ``articles.current_version_id`` FK to ``article_versions`` (added
    by the hand-edit of this migration) is dropped first so ``articles``
    no longer depends on ``article_versions``; then ``article_versions``
    goes (its only remaining FK is to ``articles``, which is fine since
    ``articles`` still exists); finally ``articles``. The FK drop is
    idempotent: skipped on DBs that predate the hand-edit. Indexes are
    dropped before their owning table; unique constraints are dropped
    implicitly via drop_table.
    """
    op.drop_index('ix_chunks_tenant_kb', table_name='chunks')
    op.drop_table('chunks')

    op.drop_index('ix_articles_tenant_kb_status', table_name='articles')
    op.drop_index('ix_articles_kb_status', table_name='articles')
    # Drop the FK only if it exists; older DBs (pre-hand-edit) never had
    # it, so downgrade must not fail on those.
    bind = op.get_bind()
    existing = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_constraint "
            "WHERE conname = 'fk_articles_current_version_id'"
        )
    ).first()
    if existing is not None:
        op.drop_constraint(
            'fk_articles_current_version_id', 'articles', type_='foreignkey'
        )

    op.drop_index(
        'ix_article_versions_article_content_hash', table_name='article_versions'
    )
    op.drop_table('article_versions')

    op.drop_table('articles')

    op.drop_table('knowledge_bases')