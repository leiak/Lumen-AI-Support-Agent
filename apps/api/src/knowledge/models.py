"""ORM models for the knowledge base (RAG) layer.

Four tables:

* ``knowledge_bases`` — a per-tenant named collection of articles.
* ``articles`` — a single source document; carries lifecycle status.
* ``article_versions`` — immutable snapshot of an article's raw text;
  every re-index creates a new version (version_number monotonically
  increases per article).
* ``chunks`` — the unit of retrieval. Tenant + knowledge_base + article
  are denormalized onto the chunk for cheap tenant-scoped RAG queries
  without joins.

Cascade is ``ON DELETE CASCADE`` everywhere so a single
``DELETE FROM tenants`` cleans up the whole subtree — this is the pattern
already used by the conversation and channel modules and matches the
test fixture's ``_delete_tenant`` helper.

Indexes:

* ``knowledge_bases``: UNIQUE (tenant_id, slug) — URL-safe identifier
  must be unique within a tenant.
* ``articles``: (tenant_id, knowledge_base_id, status) and
  (knowledge_base_id, status) — both list-by-tenant-with-status and
  list-by-KB-with-status queries are common.
* ``article_versions``: UNIQUE (article_id, version_number) plus an
  index on (article_id, content_hash) for dedup / change detection.
* ``chunks``: UNIQUE (article_version_id, chunk_index) plus an index
  on (tenant_id, knowledge_base_id) for tenant-scoped retrieval.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base
from knowledge.enums import ArticleSourceType, ArticleStatus

# Default chunking parameters for new KnowledgeBase rows.
# Source: M1 spec, section 6.1 - a token-window chunker tuned for
# English prose fed into text-embedding-3-small (8192-token context,
# roughly 5-6 pages per KB).
DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 100


class KnowledgeBase(Base):
    """A named collection of articles within a Tenant.

    Each KB pins its own embedding model + chunking parameters so that
    re-indexing under a new model can be done by spinning up a NEW KB
    rather than mutating the live one (preserves point IDs in Qdrant).
    """

    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_knowledge_bases_tenant_slug"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_model: Mapped[str] = mapped_column(
        # SQLAlchemy column-level default. The service layer always
        # passes an explicit value (resolved from ``Settings.default_embedding_model``
        # when the caller doesn't), so this default is a defensive
        # safety net for direct INSERTs (tests, migrations).
        String(100), nullable=False, default="text-embedding-3-small"
    )
    chunk_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_CHUNK_SIZE
    )
    chunk_overlap: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_CHUNK_OVERLAP
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Article(Base):
    """A source document within a KnowledgeBase.

    The body lives on ``ArticleVersion`` — every re-index creates a new
    version, so this row carries only metadata + lifecycle status.
    ``current_version_id`` is a real FK to ``article_versions.id`` but
    is nullable. Article creation proceeds in three steps: INSERT the
    article with ``current_version_id=NULL``, INSERT the version row,
    then UPDATE ``article.current_version_id`` to point at it. Because
    the column is NULL at insert time, no deferral is needed. ``ondelete``
    is ``SET NULL`` so a direct ``DELETE FROM article_versions`` doesn't
    leave a dangling pointer on the article.
    """

    __tablename__ = "articles"
    __table_args__ = (
        Index(
            "ix_articles_tenant_kb_status",
            "tenant_id",
            "knowledge_base_id",
            "status",
        ),
        Index("ix_articles_kb_status", "knowledge_base_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    knowledge_base_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source_uri: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    source_type: Mapped[ArticleSourceType] = mapped_column(
        String(20), nullable=False, default=ArticleSourceType.MANUAL
    )
    status: Mapped[ArticleStatus] = mapped_column(
        String(20), nullable=False, default=ArticleStatus.DRAFT
    )
    current_version_id: Mapped[str | None] = mapped_column(
        String(26),
        ForeignKey("article_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ArticleVersion(Base):
    """An immutable snapshot of an article's raw text.

    ``version_number`` starts at 1 and is monotonic per ``article_id``.
    ``content_hash`` is a sha256 hex of ``raw_text`` and is used for
    dedup and change detection — re-indexing the same bytes should not
    create a new version.

    ``raw_text`` is stored (not just the chunks) so the chunker can
    re-run under new parameters without going back to the source.
    """

    __tablename__ = "article_versions"
    __table_args__ = (
        UniqueConstraint(
            "article_id", "version_number", name="uq_article_versions_article_version"
        ),
        Index("ix_article_versions_article_content_hash", "article_id", "content_hash"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    article_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    # sha256 hex = 64 chars; we use Text (not String(64)) so we can swap
    # to a longer hash (sha512 = 128) without a schema change later.
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Chunk(Base):
    """A retrieval unit. One ArticleVersion produces N Chunks.

    Tenant + knowledge_base + article are denormalized onto the chunk so
    tenant-scoped RAG queries don't have to join through articles +
    versions.

    ``qdrant_point_id`` is the ID of the corresponding point in Qdrant.
    It is NULL until the embedding upsert succeeds; uniqueness on
    (article_version_id, chunk_index) is what guarantees idempotent
    re-embedding for the same version.
    """

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint(
            "article_version_id", "chunk_index", name="uq_chunks_version_index"
        ),
        Index("ix_chunks_tenant_kb", "tenant_id", "knowledge_base_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    article_version_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("article_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    knowledge_base_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    article_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    qdrant_point_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class KbMultimodalArticle(Base):
    """Stage 17 / M2.B Task 6 — metadata for an image/PDF KB article.

    The actual file bytes live in S3/MinIO (see
    :class:`knowledge.multimodal.storage.S3ObjectStore`); the vectors
    live in the new ``kb_image_vectors`` Qdrant collection.

    This table is intentionally separate from the M1 ``Article`` model
    so the M1 RAG pipeline (chunks + embeddings + indexer) stays
    untouched. Multimodal articles are a Stage 17+ capability; mixing
    the two lifecycles (status=INDEXING vs. simple upload-and-done)
    would muddy both schemas.

    PII contract: ``title`` may carry customer-meaningful text. It is
    only logged at INFO when we already have the tenant context, and
    is NEVER echoed back to a cross-tenant caller (the API never
    exposes a GET endpoint that returns this row by ID — searches go
    through Qdrant + RRF which filter on ``tenant_id``).
    """

    __tablename__ = "kb_multimodal_articles"
    __table_args__ = (
        Index(
            "idx_kb_mm_articles_tenant_kb",
            "tenant_id",
            "kb_slug",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    kb_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    file_url: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text_chunks_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    image_chunks_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class KbArticleDraft(Base):
    """Stage 18 / M2.B Task 8 — KB article draft generated from history mining.

    The ``history_mining_worker`` (Sunday cron) groups past customer
    questions via HDBSCAN, calls ``KBDraftGenerator`` to produce a
    title / body / tags, and persists one row per cluster here.

    Lifecycle::

        DRAFT  --approve-->  APPROVED  (with published_article_id set)
        DRAFT  --reject --->  REJECTED

    ``source_questions`` is a JSON list of message ULIDs — the raw
    question text is NOT stored here (it lives on the original
    ``messages`` rows). The admin GET endpoint returns a count only,
    never the IDs themselves (defense in depth: an attacker who
    compromises a tenant's admin token could otherwise pivot to the
    message table via these IDs).

    Multi-tenant: every query MUST include ``tenant_id``; cross-tenant
    access is mapped to 404 (anti-enumeration).
    """

    __tablename__ = "kb_article_drafts"
    __table_args__ = (
        Index(
            "idx_kb_drafts_tenant_status",
            "tenant_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    cluster_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_questions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    suggested_title: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_body: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_tags: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, server_default=text("'[]'")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'DRAFT'")
    )
    published_article_id: Mapped[str | None] = mapped_column(
        String(26), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(26), nullable=True)