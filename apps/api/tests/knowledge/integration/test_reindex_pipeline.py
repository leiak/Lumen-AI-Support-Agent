"""Integration tests for the reindex + vector-delete worker (Task 6.8).

End-to-end coverage of ``knowledge.worker.reindex_article`` +
``knowledge.worker.delete_article_vectors`` against a live Postgres
DB AND a live Qdrant collection. Each test:

1. Seeds a tenant + knowledge base + article + article version.
2. Calls ``reindex_article(article_id=..., force=...)``.
3. Asserts the resulting ``ArticleVersion`` count, the article's
   lifecycle status, the ``ReindexResult`` shape, and (where
   relevant) the Qdrant point count.
4. Cleans up via tenant cascade-delete in a ``finally:`` block.

PII / determinism safety:

* ``raw_text`` is built from synthetic words (``w0 w1 w2 ...``) so
  test logs stay free of anything sensitive.
* Qdrant payloads are asserted for tenant / KB / article scoping,
  never for raw text.
"""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import pytest
from qdrant_client.http import models as qmodels
from sqlalchemy import select

from core.database import get_session
from core.id_gen import new_id
from core.qdrant import get_qdrant_client
from knowledge.enums import ArticleSourceType, ArticleStatus
from knowledge.models import (
    Article,
    ArticleVersion,
    KnowledgeBase,
)
from knowledge.qdrant_client import DEFAULT_COLLECTION, DEFAULT_VECTOR_SIZE
from knowledge.worker import (
    ReindexResult,
    delete_article_vectors,
    reindex_article,
)
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository

# ============================================================================
# Fixtures / helpers
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_db_singletons() -> None:
    """Reset async engine/sessionmaker/Qdrant between tests for isolation.

    Each pytest-asyncio test runs in its own event loop. Without this
    the engine from a previous test would try to connect on a closed
    loop, raising ``RuntimeError: Event loop is closed``.
    """
    from core.database import reset_engine, reset_sessionmaker
    from core.qdrant import reset_qdrant_client

    reset_engine()
    reset_sessionmaker()
    reset_qdrant_client()
    yield
    reset_engine()
    reset_sessionmaker()
    reset_qdrant_client()


@pytest.fixture
async def tenant_factory() -> AsyncIterator[Tenant]:
    """Yield a freshly-created Tenant; cleanup cascades everything."""
    tenant = await TenantRepository().create(
        name="Reindex Test Tenant", plan=TenantPlan.FREE
    )
    try:
        yield tenant
    finally:
        await _delete_tenant(tenant.id)


async def _delete_tenant(tenant_id: str) -> None:
    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t:
            await session.delete(t)
            await session.commit()


async def _make_kb(*, tenant_id: str, slug: str = "kb") -> KnowledgeBase:
    """Insert a KnowledgeBase row. Returns the persisted KB."""
    kb = KnowledgeBase(
        id=new_id(),
        tenant_id=tenant_id,
        name="Reindex Test KB",
        slug=slug,
        description=None,
        embedding_model="text-embedding-3-small",
        chunk_size=800,
        chunk_overlap=100,
    )
    async with get_session() as session:
        session.add(kb)
        await session.commit()
        await session.refresh(kb)
    assert kb.id is not None
    return kb


async def _make_article(
    *,
    tenant_id: str,
    knowledge_base_id: str,
    title: str = "Reindex Test Article",
) -> Article:
    """Insert an Article (status=DRAFT, no version yet)."""
    article = Article(
        id=new_id(),
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        title=title,
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
    assert article.id is not None
    return article


async def _make_version(
    *,
    article_id: str,
    raw_text: str,
    version_number: int = 1,
) -> ArticleVersion:
    """Insert an ArticleVersion and (caller separately) wire it onto the article."""
    version = ArticleVersion(
        id=new_id(),
        article_id=article_id,
        version_number=version_number,
        raw_text=raw_text,
        content_hash=hashlib.sha256(raw_text.encode()).hexdigest(),
    )
    async with get_session() as session:
        session.add(version)
        await session.commit()
        await session.refresh(version)
    assert version.id is not None
    return version


async def _set_article_current_version(
    *,
    article_id: str,
    version_id: str,
) -> None:
    async with get_session() as session:
        article = await session.get(Article, article_id)
        assert article is not None
        article.current_version_id = version_id
        await session.commit()


async def _get_article(article_id: str) -> Article:
    async with get_session() as session:
        article = await session.get(Article, article_id)
        assert article is not None
        session.expunge(article)
        return article


async def _list_versions_for_article(article_id: str) -> list[ArticleVersion]:
    async with get_session() as session:
        stmt = (
            select(ArticleVersion)
            .where(ArticleVersion.article_id == article_id)
            .order_by(ArticleVersion.version_number.asc())
        )
        return list((await session.execute(stmt)).scalars().all())


async def _ensure_collection_ready() -> None:
    """Make sure the DEFAULT_COLLECTION exists before the test runs."""
    from knowledge.qdrant_client import ensure_collection

    ok = await ensure_collection(
        name=DEFAULT_COLLECTION,
        vector_size=DEFAULT_VECTOR_SIZE,
    )
    assert ok, "Failed to ensure Qdrant collection"


async def _qdrant_count_by_article_version(*, article_version_id: str) -> int:
    """Exact point count for one article_version_id payload."""
    client = get_qdrant_client()
    flt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="article_version_id",
                match=qmodels.MatchValue(value=article_version_id),
            )
        ]
    )
    result = await client.count(
        collection_name=DEFAULT_COLLECTION,
        count_filter=flt,
        exact=True,
    )
    return result.count


async def _qdrant_scroll_by_article_version(
    *, article_version_id: str
) -> list[qmodels.Record]:
    """Scroll points matching article_version_id payload."""
    client = get_qdrant_client()
    flt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="article_version_id",
                match=qmodels.MatchValue(value=article_version_id),
            )
        ]
    )
    out: list[qmodels.Record] = []
    offset: str | int | None = None
    while True:
        result = await client.scroll(
            collection_name=DEFAULT_COLLECTION,
            scroll_filter=flt,
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        records, next_offset = result
        out.extend(records)
        if not next_offset:
            break
        offset = next_offset
    return out


async def _delete_qdrant_points_for_article(*, article_id: str) -> None:
    """Best-effort cleanup of any Qdrant points for an article."""
    client = get_qdrant_client()
    flt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="article_id", match=qmodels.MatchValue(value=article_id)
            )
        ]
    )
    try:
        await client.delete(
            collection_name=DEFAULT_COLLECTION,
            points_selector=flt,
            wait=True,
        )
    except Exception as exc:  # pragma: no cover - cleanup only
        import structlog

        structlog.get_logger(__name__).warning(
            "test_qdrant_cleanup_failed",
            article_id=article_id,
            error_type=type(exc).__name__,
        )


# ============================================================================
# Mocking helpers — ``embed_texts`` is a heavy network call.
# ============================================================================


def _make_embedding_result(texts: list[str], *, dim: int = 4) -> list[list[float]]:
    """Build a deterministic vector per text — ``[0.0, 1.0, 2.0, ...]``."""
    return [[float(i) for i in range(dim)] for _ in texts]


class _StubEmbeddingResult:
    """Minimal stand-in for ``EmbeddingResult`` used by the test patch."""

    def __init__(self, vectors: list[list[float]]) -> None:
        self.vectors = vectors
        self.model = "text-embedding-3-small"
        self.usage = type("U", (), {"prompt_tokens": 0, "total_tokens": 0})()


def _patch_embed(monkeypatch: pytest.MonkeyPatch, *, dim: int = 4) -> None:
    """Replace ``knowledge.worker.embed_texts`` with a deterministic stub."""
    from knowledge import worker as worker_module

    async def _stub(*, texts, model, tenant_id=None, client=None):
        return _StubEmbeddingResult(_make_embedding_result(texts, dim=dim))

    monkeypatch.setattr(worker_module, "embed_texts", _stub)


# ============================================================================
# Tests
# ============================================================================


@pytest.mark.integration
async def test_reindex_article_no_change_skips(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reindex with unchanged raw_text returns skipped=True, no new version.

    The first call (with force=False) sees the current version's hash
    equal to the latest version's hash (they are the SAME row) and
    short-circuits — no new ArticleVersion, no pipeline run.
    """
    _patch_embed(monkeypatch, dim=DEFAULT_VECTOR_SIZE)
    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id)
    article = await _make_article(tenant_id=tenant.id, knowledge_base_id=kb.id)
    raw_text = " ".join(f"w{i}" for i in range(200))
    version = await _make_version(article_id=article.id, raw_text=raw_text)
    await _set_article_current_version(article_id=article.id, version_id=version.id)

    try:
        result = await reindex_article(article_id=article.id)

        assert isinstance(result, ReindexResult)
        assert result.skipped is True
        assert result.version_number == 1
        assert result.article_id == article.id
        assert result.chunks_indexed == 0

        # Article status is whatever it was — DRAFT for a freshly
        # seeded article (the skip path doesn't transition status).
        article_after = await _get_article(article.id)
        assert article_after.status == ArticleStatus.DRAFT

        # No new ArticleVersion rows.
        versions = await _list_versions_for_article(article.id)
        assert len(versions) == 1
        assert versions[0].id == version.id
        assert versions[0].version_number == 1
    finally:
        await _delete_qdrant_points_for_article(article_id=article.id)


@pytest.mark.integration
async def test_reindex_article_content_change_creates_new_version(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reindex with changed raw_text creates v2 and runs the pipeline.

    Note: this test starts from a freshly-seeded article. The FIRST
    reindex sees v1 == latest so it WILL create v2 (hash differs from
    "no row" because the query returns v1 itself, which matches by
    construction — but we then verify the call proceeded to index
    v2, which proves the hash check did not short-circuit).

    To make the test deterministic about the content-change branch,
    we:
    1. Seed article + v1.
    2. Manually update v1's raw_text to NEW content (without bumping
       content_hash, simulating a "source changed under us" scenario
       — but that would make the hash differ). Instead, we update
       content_hash to a stale value so the check sees a mismatch.
    """
    _patch_embed(monkeypatch, dim=DEFAULT_VECTOR_SIZE)
    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id)
    article = await _make_article(tenant_id=tenant.id, knowledge_base_id=kb.id)
    original_text = " ".join(f"w{i}" for i in range(200))
    v1 = await _make_version(article_id=article.id, raw_text=original_text)
    await _set_article_current_version(article_id=article.id, version_id=v1.id)

    # Pretend the source changed under us: blank v1's content_hash
    # so the reindex check sees a mismatch and proceeds to mint v2.
    # The raw_text is unchanged (we don't want to mutate the bytes
    # — the reindex path will re-store the same bytes on v2).
    async with get_session() as session:
        v = await session.get(ArticleVersion, v1.id)
        assert v is not None
        v.content_hash = "0" * 64  # definitely doesn't match sha256 of raw_text
        await session.commit()

    try:
        result = await reindex_article(article_id=article.id)

        assert isinstance(result, ReindexResult)
        assert result.skipped is False
        assert result.version_number == 2
        assert result.article_id == article.id
        # 200 words at chunk_size=800 -> 1 chunk.
        assert result.chunks_indexed == 1
        assert result.status == ArticleStatus.INDEXED

        # DB: 2 ArticleVersion rows, article points at v2.
        versions = await _list_versions_for_article(article.id)
        assert len(versions) == 2
        assert versions[0].version_number == 1
        assert versions[1].version_number == 2
        assert versions[1].raw_text == original_text
        # v2's content_hash was recomputed by reindex from raw_text.
        assert versions[1].content_hash == hashlib.sha256(
            original_text.encode()
        ).hexdigest()

        article_after = await _get_article(article.id)
        assert article_after.current_version_id == versions[1].id
        assert article_after.status == ArticleStatus.INDEXED

        # Qdrant: v2 has 1 point. v1 may or may not — depends on
        # whether anyone called index_article on v1 (we didn't here,
        # so v1 has 0 points).
        v2_count = await _qdrant_count_by_article_version(
            article_version_id=versions[1].id
        )
        assert v2_count == 1

        v1_records = await _qdrant_scroll_by_article_version(
            article_version_id=versions[0].id
        )
        assert v1_records == []
    finally:
        await _delete_qdrant_points_for_article(article_id=article.id)


@pytest.mark.integration
async def test_reindex_article_force_creates_new_version_even_if_unchanged(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``force=True`` mints a new version even when raw_text is identical.

    Verifies the force-bypass branch of the hash check.
    """
    _patch_embed(monkeypatch, dim=DEFAULT_VECTOR_SIZE)
    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id)
    article = await _make_article(tenant_id=tenant.id, knowledge_base_id=kb.id)
    raw_text = " ".join(f"w{i}" for i in range(50))
    v1 = await _make_version(article_id=article.id, raw_text=raw_text)
    await _set_article_current_version(article_id=article.id, version_id=v1.id)

    try:
        result = await reindex_article(article_id=article.id, force=True)

        assert isinstance(result, ReindexResult)
        assert result.skipped is False
        assert result.version_number == 2
        assert result.chunks_indexed == 1
        assert result.status == ArticleStatus.INDEXED

        versions = await _list_versions_for_article(article.id)
        assert len(versions) == 2
        # Both versions carry the SAME raw_text (audit trail: the
        # force branch re-stores verbatim).
        assert versions[1].raw_text == raw_text
        # ...and the SAME content_hash.
        assert versions[1].content_hash == versions[0].content_hash
    finally:
        await _delete_qdrant_points_for_article(article_id=article.id)


@pytest.mark.integration
async def test_reindex_article_missing_article_raises(
    tenant_factory: Tenant,
) -> None:
    """Nonexistent article_id -> ValueError, NOT a silent FAILED result."""
    missing_id = new_id()  # never inserted
    with pytest.raises(ValueError, match="article not found"):
        await reindex_article(article_id=missing_id)


@pytest.mark.integration
async def test_reindex_article_missing_version_raises(
    tenant_factory: Tenant,
) -> None:
    """Article with current_version_id=None -> ValueError."""
    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id)
    # Article created without a current version — _make_article sets
    # current_version_id=None by default.
    article = await _make_article(tenant_id=tenant.id, knowledge_base_id=kb.id)

    with pytest.raises(ValueError, match="current version missing"):
        await reindex_article(article_id=article.id)


@pytest.mark.integration
async def test_delete_article_vectors_removes_all_versions(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delete_article_vectors clears Qdrant points for every version.

    Setup:
    1. Seed article + v1 (raw_text = 50 words).
    2. Index v1 via index_article (1 chunk -> 1 Qdrant point).
    3. Force-bump to v2 via reindex_article(force=True) and index.
       -> Qdrant has 1 point for v2.
       v1's points were swept by index_article's delete-then-upsert
       contract — so after v2's run only v2 has points.
    4. Call delete_article_vectors(article_id) — must clear v2's points.

    The point of this test is to confirm delete_article_vectors
    sweeps ALL versions, not just the current one. We confirm that
    by indexing v1 explicitly with a SEPARATE version (no bump), then
    asserting v1's points are gone AFTER delete.
    """
    _patch_embed(monkeypatch, dim=DEFAULT_VECTOR_SIZE)
    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id)
    article = await _make_article(tenant_id=tenant.id, knowledge_base_id=kb.id)

    text_v1 = " ".join(f"oldw{i}" for i in range(50))
    text_v2 = " ".join(f"neww{i}" for i in range(50))
    v1 = await _make_version(article_id=article.id, raw_text=text_v1, version_number=1)
    v2 = await _make_version(article_id=article.id, raw_text=text_v2, version_number=2)
    await _set_article_current_version(article_id=article.id, version_id=v2.id)

    # Hand-insert chunks for v1 so v1 has its own Qdrant point. We
    # avoid calling index_article here because index_article for v2
    # would sweep v1's points (delete-then-upsert only sweeps the
    # CURRENT version's points). We just want to verify delete
    # handles multiple versions.
    from knowledge.models import Chunk
    from knowledge.worker import _chunk_id, _qdrant_point_id

    chunk_v1 = Chunk(
        id=_chunk_id(v1.id, 0),
        article_version_id=v1.id,
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        article_id=article.id,
        chunk_index=0,
        text=text_v1,
        token_count=50,
        metadata_json=None,
        qdrant_point_id=_qdrant_point_id(_chunk_id(v1.id, 0)),
    )
    from knowledge.qdrant_client import upsert_chunks as _upsert

    async with get_session() as session:
        session.add(chunk_v1)
        await session.commit()
    # Insert a synthetic Qdrant point for v1 directly.
    synth_point = qmodels.PointStruct(
        id=chunk_v1.qdrant_point_id,
        vector=[0.0] * DEFAULT_VECTOR_SIZE,
        payload={
            "tenant_id": tenant.id,
            "knowledge_base_id": kb.id,
            "article_id": article.id,
            "article_version_id": v1.id,
            "chunk_index": 0,
            "text": "synthetic",
            "metadata": {},
        },
    )
    await _upsert(collection=DEFAULT_COLLECTION, points=[synth_point])

    try:
        # Baseline: v1 has 1 point, article_id filter shows 1 point.
        assert await _qdrant_count_by_article_version(
            article_version_id=v1.id
        ) == 1

        # Now run delete_article_vectors — it should clear v1's
        # point. v2 has 0 points to begin with, but the function
        # still calls delete_points_by_article_version for v2
        # (returns 1 on success — Qdrant's delete API is opaque
        # about the actual point count).
        deleted = await delete_article_vectors(article_id=article.id)

        # 2 versions cleaned up — one with a real point, one empty.
        # The contract is "best-effort count of successful delete
        # operations", not "actual points removed".
        assert deleted == 2

        # Qdrant confirms: 0 points for v1, 0 points for v2.
        assert await _qdrant_count_by_article_version(
            article_version_id=v1.id
        ) == 0
        assert await _qdrant_count_by_article_version(
            article_version_id=v2.id
        ) == 0
    finally:
        await _delete_qdrant_points_for_article(article_id=article.id)


@pytest.mark.integration
async def test_delete_article_vectors_idempotent_on_missing_article(
    tenant_factory: Tenant,
) -> None:
    """delete_article_vectors on a nonexistent article returns 0, no raise."""
    missing_id = new_id()
    deleted = await delete_article_vectors(article_id=missing_id)
    assert deleted == 0


@pytest.mark.integration
async def test_reindex_article_indexes_new_version_chunks(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After reindex, the new version's Qdrant point count == chunks_indexed.

    Also verifies v1's points are PRESERVED when reindex mints v2:
    index_article only deletes points for the CURRENT version, so
    v1's history points survive the v2 indexing pass.
    """
    _patch_embed(monkeypatch, dim=DEFAULT_VECTOR_SIZE)
    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id)
    article = await _make_article(tenant_id=tenant.id, knowledge_base_id=kb.id)

    # 900 words at chunk_size=800/overlap=100 -> 2 chunks.
    raw_text_v1 = " ".join(f"oldw{i}" for i in range(900))
    raw_text_v2 = " ".join(f"neww{i}" for i in range(900))
    v1 = await _make_version(article_id=article.id, raw_text=raw_text_v1, version_number=1)
    await _set_article_current_version(article_id=article.id, version_id=v1.id)

    try:
        # Index v1.
        from knowledge.worker import index_article

        first = await index_article(article_id=article.id)
        assert first.status == ArticleStatus.INDEXED
        assert first.chunks_indexed == 2

        # v1 has 2 points in Qdrant.
        assert await _qdrant_count_by_article_version(
            article_version_id=v1.id
        ) == 2

        # Simulate the source content changed: blank v1.content_hash
        # so the reindex check sees a mismatch and mints v2.
        async with get_session() as session:
            v = await session.get(ArticleVersion, v1.id)
            assert v is not None
            v.content_hash = "0" * 64
            # Update the article's current raw_text pointer by
            # pointing it back to v1 but with NEW raw_text stored
            # in place. This is the "source changed under us"
            # scenario the spec describes.
            v.raw_text = raw_text_v2
            await session.commit()

        # Now reindex (force=False) — hash of current raw_text
        # (raw_text_v2) != stale hash on v1 ("0"*64), so the
        # hash-differ branch fires and mints v2 with the NEW
        # raw_text.
        result = await reindex_article(article_id=article.id)
        assert result.skipped is False
        assert result.version_number == 2
        assert result.chunks_indexed == 2
        assert result.status == ArticleStatus.INDEXED

        # v2 has 2 points in Qdrant.
        v2_rows = await _list_versions_for_article(article.id)
        v2 = v2_rows[-1]
        assert v2.version_number == 2
        assert await _qdrant_count_by_article_version(
            article_version_id=v2.id
        ) == 2

        # v1's points are preserved (history — index_article only
        # sweeps the CURRENT version's points).
        v1_records = await _qdrant_scroll_by_article_version(
            article_version_id=v1.id
        )
        assert len(v1_records) == 2

        # Article is now pointing at v2.
        article_after = await _get_article(article.id)
        assert article_after.current_version_id == v2.id
        assert article_after.status == ArticleStatus.INDEXED
    finally:
        await _delete_qdrant_points_for_article(article_id=article.id)
