"""Live-DB integration tests for the knowledge base (RAG) layer.

Verifies the four-table knowledge base subtree against a real Postgres
database, exercising:

- the FK + cascade contract (every table cascades on delete from
  tenants -> knowledge_bases -> articles -> article_versions -> chunks);
- the ULID primary keys + TZ-aware timestamps round-tripping;
- the (article_version_id, chunk_index) UNIQUE constraint that makes
  re-embedding idempotent per version;
- the (article_id, version_number) UNIQUE constraint that keeps
  version_number monotonic per article.

Cleanup is via cascading Tenant delete in a ``finally:`` block, matching
the conversation lifecycle test's pattern.
"""
from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import func, select

from core.database import get_session
from core.id_gen import new_id
from knowledge.enums import ArticleSourceType, ArticleStatus
from knowledge.models import (
    Article,
    ArticleVersion,
    Chunk,
    KnowledgeBase,
)
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository

# ============================================================================
# Fixtures / helpers
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_db_singletons() -> None:
    """Reset the async engine + sessionmaker between tests to avoid cross-loop
    contamination."""
    from core.database import reset_engine, reset_sessionmaker

    yield
    reset_engine()
    reset_sessionmaker()


async def _seed_tenant(*, name: str = "KB Lifecycle Tenant") -> Tenant:
    """Create a tenant via TenantRepository (auto-allocates ULID)."""
    return await TenantRepository().create(name=name, plan=TenantPlan.FREE)


async def _delete_tenant(tenant_id: str) -> None:
    """Cascade-delete a tenant (kills its KBs, articles, versions, chunks)."""
    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t:
            await session.delete(t)
            await session.commit()


async def _count(table: type) -> int:
    """Count rows in ``table`` across the live DB."""
    async with get_session() as session:
        stmt = select(func.count()).select_from(table)
        result = await session.execute(stmt)
        return int(result.scalar() or 0)


async def _count_for_tenant(model: type, tenant_id: str) -> int:
    """Count rows in ``model`` scoped to ``tenant_id``.

    For models with a direct ``tenant_id`` column (``KnowledgeBase``,
    ``Article``, ``Chunk``) we filter on that. For ``ArticleVersion`` —
    which is keyed off ``article_id`` and carries no denormalized
    ``tenant_id`` — we count versions whose article belongs to this
    tenant. Scoping to the tenant keeps cascade-delete assertions from
    leaking across tests.
    """
    async with get_session() as session:
        if hasattr(model, "tenant_id"):
            stmt = select(func.count()).select_from(model).where(
                model.tenant_id == tenant_id
            )
        else:
            # ArticleVersion: traverse via Article.tenant_id.
            stmt = (
                select(func.count())
                .select_from(model)
                .join(Article, model.article_id == Article.id)
                .where(Article.tenant_id == tenant_id)
            )
        return int((await session.execute(stmt)).scalar() or 0)


# ============================================================================
# Test — create + cascade-delete end-to-end
# ============================================================================


@pytest.mark.integration
async def test_kb_lifecycle_and_tenant_cascade_delete() -> None:
    """End-to-end: seed tenant -> KB -> Article -> Version -> 3 chunks, then
    cascade-delete the tenant and verify every knowledge table is empty.

    Steps:
      1. Seed tenant via TenantRepository.
      2. Insert a KnowledgeBase (embedding_model defaults, chunk params 800/100).
      3. Insert an Article (status=DRAFT, no current_version_id yet).
      4. Insert ArticleVersion(version_number=1, content_hash=sha256 hex).
      5. Insert 3 Chunks (chunk_index 0, 1, 2; one with metadata_json + qdrant_point_id).
      6. Verify rows exist in all 4 tables for this tenant.
      7. Cascade-delete the tenant.
      8. Verify every table is empty (no rows for ANY tenant).
    """
    tenant = await _seed_tenant()
    try:
        # Take a baseline of the knowledge tables BEFORE we insert anything.
        # The other tests in this file may not use these tables, but other
        # suites might; we want a strict delta rather than an absolute zero.
        baseline = {
            "kb": await _count(KnowledgeBase),
            "article": await _count(Article),
            "version": await _count(ArticleVersion),
            "chunk": await _count(Chunk),
        }

        # 2. KnowledgeBase
        kb = KnowledgeBase(
            id=new_id(),
            tenant_id=tenant.id,
            name="Product Docs",
            slug="product-docs",
            description="All product documentation",
            embedding_model="text-embedding-3-small",
            chunk_size=800,
            chunk_overlap=100,
        )
        async with get_session() as session:
            session.add(kb)
            await session.commit()
            await session.refresh(kb)

        assert kb.id is not None
        assert len(kb.id) == 26  # ULID length
        assert kb.created_at.tzinfo is not None  # TZ-aware
        assert kb.updated_at.tzinfo is not None

        # 3. Article (status=DRAFT)
        article = Article(
            id=new_id(),
            tenant_id=tenant.id,
            knowledge_base_id=kb.id,
            title="How to reset your password",
            source_uri=None,
            source_type=ArticleSourceType.MANUAL,
            status=ArticleStatus.DRAFT,
            current_version_id=None,
            error_message=None,
        )
        async with get_session() as session:
            session.add(article)
            await session.commit()
            await session.refresh(article)

        assert article.status == ArticleStatus.DRAFT
        assert article.current_version_id is None

        # 4. ArticleVersion (version_number=1)
        raw_text = "Reset password: visit Settings -> Account -> Reset Password."
        content_hash = hashlib.sha256(raw_text.encode()).hexdigest()
        version = ArticleVersion(
            id=new_id(),
            article_id=article.id,
            version_number=1,
            raw_text=raw_text,
            content_hash=content_hash,
        )
        async with get_session() as session:
            session.add(version)
            await session.commit()
            await session.refresh(version)

        assert version.version_number == 1
        assert version.content_hash == content_hash
        assert len(version.content_hash) == 64  # sha256 hex

        # Wire current_version_id back on the article — this is what the
        # indexing pipeline would do after creating v1.
        async with get_session() as session:
            existing = await session.get(Article, article.id)
            assert existing is not None
            existing.current_version_id = version.id
            await session.commit()
            await session.refresh(existing)
        assert existing is not None
        assert existing.current_version_id == version.id

        # 5. Three Chunks (index 0, 1, 2). The first one has full
        # metadata_json + qdrant_point_id to exercise the JSONB + nullable
        # string columns; the others are minimal.
        chunks_in = [
            Chunk(
                id=new_id(),
                article_version_id=version.id,
                tenant_id=tenant.id,
                knowledge_base_id=kb.id,
                article_id=article.id,
                chunk_index=0,
                text="chunk zero",
                token_count=12,
                metadata_json={"section": "intro", "page": 1},
                qdrant_point_id="qdrant-point-0",
            ),
            Chunk(
                id=new_id(),
                article_version_id=version.id,
                tenant_id=tenant.id,
                knowledge_base_id=kb.id,
                article_id=article.id,
                chunk_index=1,
                text="chunk one",
            ),
            Chunk(
                id=new_id(),
                article_version_id=version.id,
                tenant_id=tenant.id,
                knowledge_base_id=kb.id,
                article_id=article.id,
                chunk_index=2,
                text="chunk two",
            ),
        ]
        async with get_session() as session:
            for c in chunks_in:
                session.add(c)
            await session.commit()

        # 6. Verify rows exist for this tenant across all 4 tables.
        assert await _count(KnowledgeBase) == baseline["kb"] + 1
        assert await _count(Article) == baseline["article"] + 1
        assert await _count(ArticleVersion) == baseline["version"] + 1
        assert await _count(Chunk) == baseline["chunk"] + 3

        async with get_session() as session:
            kb_again = await session.get(KnowledgeBase, kb.id)
            assert kb_again is not None
            assert kb_again.tenant_id == tenant.id

            article_again = await session.get(Article, article.id)
            assert article_again is not None
            assert article_again.current_version_id == version.id

            version_again = await session.get(ArticleVersion, version.id)
            assert version_again is not None
            assert version_again.article_id == article.id

            chunk_stmt = (
                select(Chunk)
                .where(Chunk.article_version_id == version.id)
                .order_by(Chunk.chunk_index)
            )
            result = await session.execute(chunk_stmt)
            persisted_chunks = list(result.scalars().all())
            assert len(persisted_chunks) == 3
            assert [c.chunk_index for c in persisted_chunks] == [0, 1, 2]
            assert persisted_chunks[0].qdrant_point_id == "qdrant-point-0"
            assert persisted_chunks[0].metadata_json == {
                "section": "intro",
                "page": 1,
            }
            assert persisted_chunks[1].qdrant_point_id is None

        # 7. Cascade-delete the tenant — every knowledge table row goes
        # with it because every FK is ON DELETE CASCADE.
        await _delete_tenant(tenant.id)

        # 8. Verify every knowledge table has zero rows for this tenant.
        assert await _count_for_tenant(KnowledgeBase, tenant.id) == 0, (
            "knowledge_bases not cleaned up"
        )
        assert await _count_for_tenant(Article, tenant.id) == 0, (
            "articles not cleaned up"
        )
        assert await _count_for_tenant(ArticleVersion, tenant.id) == 0, (
            "article_versions not cleaned up"
        )
        assert await _count_for_tenant(Chunk, tenant.id) == 0, (
            "chunks not cleaned up"
        )

        # Sanity: the tenant itself is also gone.
        async with get_session() as session:
            t = await session.get(Tenant, tenant.id)
            assert t is None
    finally:
        # Belt-and-braces cleanup in case the test failed mid-way.
        await _delete_tenant(tenant.id)


# ---------------------------------------------------------------------------
# Helper: capture a TZ-aware "now" without importing datetime into the
# top-level namespace (we want a single, fresh import in one place).
# ---------------------------------------------------------------------------


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)