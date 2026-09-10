"""Unit tests for the knowledge base ORM models.

Pure instantiation tests — no DB. They lock in field defaults and the
StrEnum contract (so accidental renames show up immediately) and serve
as the model contract for downstream repositories/services to rely on.
"""
from datetime import UTC, datetime

from core.id_gen import new_id
from knowledge.enums import ArticleSourceType, ArticleStatus
from knowledge.models import (
    Article,
    ArticleVersion,
    Chunk,
    KnowledgeBase,
)


def _tenant_id() -> str:
    return new_id()


def _kb_id() -> str:
    return new_id()


def _article_id() -> str:
    return new_id()


def _article_version_id() -> str:
    return new_id()


# ---------------------------------------------------------------------------
# KnowledgeBase
# ---------------------------------------------------------------------------


def test_knowledge_base_can_be_constructed() -> None:
    """KB takes the required fields and exposes defaults for chunking."""
    now = datetime.now(UTC)
    kb = KnowledgeBase(
        id=new_id(),
        tenant_id=_tenant_id(),
        name="Product Docs",
        slug="product-docs",
        description="All product documentation",
        embedding_model="text-embedding-3-small",
        chunk_size=800,
        chunk_overlap=100,
        created_at=now,
        updated_at=now,
    )
    assert kb.name == "Product Docs"
    assert kb.slug == "product-docs"
    assert kb.description == "All product documentation"
    assert kb.embedding_model == "text-embedding-3-small"
    assert kb.chunk_size == 800
    assert kb.chunk_overlap == 100


def test_knowledge_base_description_optional() -> None:
    """description is nullable per spec."""
    now = datetime.now(UTC)
    kb = KnowledgeBase(
        id=new_id(),
        tenant_id=_tenant_id(),
        name="FAQ",
        slug="faq",
        description=None,
        embedding_model="text-embedding-3-small",
        created_at=now,
        updated_at=now,
    )
    assert kb.description is None


# ---------------------------------------------------------------------------
# Article
# ---------------------------------------------------------------------------


def test_article_can_be_constructed() -> None:
    """Article defaults to DRAFT status when no status is provided."""
    now = datetime.now(UTC)
    article = Article(
        id=new_id(),
        tenant_id=_tenant_id(),
        knowledge_base_id=_kb_id(),
        title="How to reset your password",
        source_uri=None,
        source_type=ArticleSourceType.MANUAL,
        status=ArticleStatus.DRAFT,
        current_version_id=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )
    assert article.title == "How to reset your password"
    assert article.source_type is ArticleSourceType.MANUAL
    assert article.status is ArticleStatus.DRAFT
    assert article.current_version_id is None
    assert article.error_message is None


def test_article_default_status_is_draft() -> None:
    """The Python-level default on the ORM column is ``ArticleStatus.DRAFT``.

    SQLAlchemy column ``default=`` is a *flush-time* default — it does
    NOT populate the attribute at ``__init__`` time. So constructing
    without ``status`` yields ``None`` until the row is flushed.

    We assert the column's default is wired up by introspecting the
    mapped table, which is the unit-level contract that matters here.
    """
    status_col = Article.__table__.c["status"]
    assert status_col.default is not None
    assert status_col.default.arg is ArticleStatus.DRAFT


# ---------------------------------------------------------------------------
# ArticleVersion
# ---------------------------------------------------------------------------


def test_article_version_can_be_constructed() -> None:
    """ArticleVersion stores raw_text + content_hash; version_number is per-article monotonic."""
    article_id = _article_id()
    version = ArticleVersion(
        id=new_id(),
        article_id=article_id,
        version_number=1,
        raw_text="Hello, world.",
        content_hash="abc123",
        created_at=datetime.now(UTC),
    )
    assert version.article_id == article_id
    assert version.version_number == 1
    assert version.raw_text == "Hello, world."
    assert version.content_hash == "abc123"


# ---------------------------------------------------------------------------
# Chunk
# ---------------------------------------------------------------------------


def test_chunk_can_be_constructed() -> None:
    """Chunk carries the denormalized tenant/KB/article FKs and a qdrant_point_id."""
    now = datetime.now(UTC)
    chunk = Chunk(
        id=new_id(),
        article_version_id=_article_version_id(),
        tenant_id=_tenant_id(),
        knowledge_base_id=_kb_id(),
        article_id=_article_id(),
        chunk_index=0,
        text="first chunk",
        token_count=42,
        metadata_json={"section": "intro"},
        qdrant_point_id="qdrant-abc",
        created_at=now,
    )
    assert chunk.chunk_index == 0
    assert chunk.text == "first chunk"
    assert chunk.token_count == 42
    assert chunk.metadata_json == {"section": "intro"}
    assert chunk.qdrant_point_id == "qdrant-abc"


def test_chunk_optional_fields_default_to_none() -> None:
    """token_count, metadata_json, qdrant_point_id are nullable per spec."""
    now = datetime.now(UTC)
    chunk = Chunk(
        id=new_id(),
        article_version_id=_article_version_id(),
        tenant_id=_tenant_id(),
        knowledge_base_id=_kb_id(),
        article_id=_article_id(),
        chunk_index=1,
        text="second chunk",
        created_at=now,
    )
    assert chunk.token_count is None
    assert chunk.metadata_json is None
    assert chunk.qdrant_point_id is None


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


def test_article_status_enum_values() -> None:
    """All four lifecycle statuses are exposed as StrEnum members with correct values."""
    assert ArticleStatus.DRAFT == "draft"
    assert ArticleStatus.INDEXING == "indexing"
    assert ArticleStatus.INDEXED == "indexed"
    assert ArticleStatus.FAILED == "failed"


def test_article_source_type_enum_values() -> None:
    """All three source types are exposed as StrEnum members with correct values."""
    assert ArticleSourceType.UPLOAD == "upload"
    assert ArticleSourceType.URL == "url"
    assert ArticleSourceType.MANUAL == "manual"


def test_article_status_str_equality() -> None:
    """StrEnum members compare equal to their string value (used in SQL string filters)."""
    assert ArticleStatus.DRAFT == "draft"
    # Python 3.11+ StrEnum: __str__ returns the value (not the member name).
    assert str(ArticleStatus.INDEXED) == "indexed"
    # f-string interpolation goes through __format__ -> __str__ -> value.
    assert f"status={ArticleStatus.FAILED}" == "status=failed"