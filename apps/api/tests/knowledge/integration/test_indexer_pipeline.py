"""Integration tests for the knowledge base indexer pipeline (Task 6.7).

End-to-end coverage of ``knowledge.worker.index_article`` against a
live Postgres database AND a live Qdrant collection. Each test:

1. Seeds a tenant + knowledge base + article + article version.
2. Calls ``index_article(article_id=...)``.
3. Asserts the final ``Article.status`` + ``Chunk`` row counts +
   matching Qdrant point counts (using a tenant-scoped scroll
   filter).
4. Cleans up via tenant cascade-delete in a ``finally:`` block.

PII / determinism safety:

* ``raw_text`` is built from synthetic words (``w0 w1 w2 ...``) so
  we can grep the test logs for "w0" without matching anything real.
* Point IDs are ULIDs minted by ``core.id_gen.new_id()`` — we
  explicitly assert two consecutive runs produce the SAME point ID
  set, which is the contract the 6.11 retriever relies on.
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
    Chunk,
    KnowledgeBase,
)
from knowledge.qdrant_client import DEFAULT_COLLECTION, DEFAULT_VECTOR_SIZE
from knowledge.worker import index_article
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
        name="Indexer Test Tenant", plan=TenantPlan.FREE
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
        name="Test KB",
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
    title: str = "Test Article",
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
        # Detach so the caller can read attrs after the session closes.
        session.expunge(article)
        return article


async def _list_chunks_for_version(version_id: str) -> list[Chunk]:
    async with get_session() as session:
        stmt = (
            select(Chunk)
            .where(Chunk.article_version_id == version_id)
            .order_by(Chunk.chunk_index)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def _ensure_collection_ready() -> None:
    """Make sure the DEFAULT_COLLECTION exists before the test runs.

    The worker assumes the collection exists (it's ensured at API
    startup). We mirror that here so an isolated test run still works.
    """
    from knowledge.qdrant_client import ensure_collection

    ok = await ensure_collection(
        name=DEFAULT_COLLECTION,
        vector_size=DEFAULT_VECTOR_SIZE,
    )
    assert ok, "Failed to ensure Qdrant collection"


async def _qdrant_scroll_by_article(
    *, article_id: str
) -> list[qmodels.Record]:
    """Scroll points matching article_id payload (no tenant filter —
    the article_id is already tenant-unique)."""
    client = get_qdrant_client()
    flt = qmodels.Filter(
        must=[qmodels.FieldCondition(key="article_id", match=qmodels.MatchValue(value=article_id))]
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
        # Best-effort cleanup; a transient Qdrant error during teardown
        # shouldn't fail the test that just passed.
        import structlog

        structlog.get_logger(__name__).warning(
            "test_qdrant_cleanup_failed",
            article_id=article_id,
            error_type=type(exc).__name__,
        )


# ============================================================================
# Mocking helpers — ``embed_texts`` is a heavy network call. We never
# want the test suite to depend on a live OpenAI key, so every test
# patches the worker module's lazy import target.
# ============================================================================


def _make_embedding_result(texts: list[str], *, dim: int = 4) -> list[list[float]]:
    """Build a deterministic vector per text — ``[0.0, 1.0, 2.0, ...]``.

    Deterministic vectors make the test log readable: a vector
    accidentally logged (it never should be — PII) would not look
    like a real OpenAI embedding.
    """
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


def _patch_embed_fail(
    monkeypatch: pytest.MonkeyPatch, *, exc: Exception
) -> None:
    """Replace ``knowledge.worker.embed_texts`` with one that raises ``exc``."""
    from knowledge import worker as worker_module
    from llm_client.types import EmbeddingError

    async def _stub(*, texts, model, tenant_id=None, client=None):
        if isinstance(exc, EmbeddingError):
            raise exc
        raise EmbeddingError("forced test failure") from exc

    monkeypatch.setattr(worker_module, "embed_texts", _stub)


def _patch_qdrant_upsert_return_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``upsert_chunks`` with a stub returning 0."""
    from knowledge import worker as worker_module

    async def _stub(*, collection, points):
        return 0

    monkeypatch.setattr(worker_module, "upsert_chunks", _stub)


# ============================================================================
# Tests
# ============================================================================


@pytest.mark.integration
async def test_index_article_happy_path(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: a 200-word article indexes to 1 chunk + 1 Qdrant point.

    200 words < default chunk_size (800), so chunker emits exactly
    one chunk. The assertion is exact (== 1) so a regression that
    doubles the chunk count shows up immediately.
    """
    await _ensure_collection_ready()
    _patch_embed(monkeypatch, dim=DEFAULT_VECTOR_SIZE)
    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id)
    article = await _make_article(tenant_id=tenant.id, knowledge_base_id=kb.id)
    # Use synthetic words so the test log stays PII-safe.
    raw_text = " ".join(f"w{i}" for i in range(200))
    version = await _make_version(article_id=article.id, raw_text=raw_text)
    await _set_article_current_version(
        article_id=article.id, version_id=version.id
    )

    try:
        result = await index_article(article_id=article.id)
        assert result.status == ArticleStatus.INDEXED
        assert result.chunks_indexed == 1

        # DB: article status updated, error_message cleared, 1 chunk.
        article_after = await _get_article(article.id)
        assert article_after.status == ArticleStatus.INDEXED
        assert article_after.error_message is None

        chunks = await _list_chunks_for_version(version.id)
        assert len(chunks) == 1
        assert chunks[0].chunk_index == 0
        assert chunks[0].qdrant_point_id is not None
        # Qdrant point IDs are UUIDs (server 1.13 only accepts int or UUID).
        assert len(chunks[0].qdrant_point_id) == 36
        assert chunks[0].token_count == 200

        # Qdrant: exactly 1 point matching article_id, with the
        # expected payload fields.
        records = await _qdrant_scroll_by_article(article_id=article.id)
        assert len(records) == 1
        payload = records[0].payload or {}
        assert payload.get("tenant_id") == tenant.id
        assert payload.get("knowledge_base_id") == kb.id
        assert payload.get("article_id") == article.id
        assert payload.get("article_version_id") == version.id
        assert payload.get("chunk_index") == 0
    finally:
        await _delete_qdrant_points_for_article(article_id=article.id)


@pytest.mark.integration
async def test_index_article_idempotent(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling index_article twice yields the SAME Chunk count + SAME point IDs.

    Verifies the delete-then-upsert + delete-then-insert idempotency
    contract: re-running the worker must not duplicate rows or points.
    """
    await _ensure_collection_ready()
    _patch_embed(monkeypatch, dim=DEFAULT_VECTOR_SIZE)
    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id)
    article = await _make_article(tenant_id=tenant.id, knowledge_base_id=kb.id)
    raw_text = " ".join(f"w{i}" for i in range(200))
    version = await _make_version(article_id=article.id, raw_text=raw_text)
    await _set_article_current_version(
        article_id=article.id, version_id=version.id
    )

    try:
        first = await index_article(article_id=article.id)
        assert first.status == ArticleStatus.INDEXED
        chunks_first = await _list_chunks_for_version(version.id)
        assert len(chunks_first) == 1
        first_point_ids = {c.qdrant_point_id for c in chunks_first}
        assert all(pid is not None for pid in first_point_ids)

        # Qdrant has 1 point, with the same point ID we just persisted.
        records_first = await _qdrant_scroll_by_article(article_id=article.id)
        assert len(records_first) == 1
        first_qdrant_ids = {r.id for r in records_first}
        assert first_qdrant_ids == first_point_ids

        # Second run — must be a no-op (same counts, same IDs).
        second = await index_article(article_id=article.id)
        assert second.status == ArticleStatus.INDEXED
        assert second.chunks_indexed == first.chunks_indexed

        chunks_second = await _list_chunks_for_version(version.id)
        assert len(chunks_second) == len(chunks_first)
        second_point_ids = {c.qdrant_point_id for c in chunks_second}
        # SAME point IDs — the deterministic mapping is the whole
        # point of the contract.
        assert second_point_ids == first_point_ids

        records_second = await _qdrant_scroll_by_article(article_id=article.id)
        assert len(records_second) == 1
        assert {r.id for r in records_second} == first_point_ids
    finally:
        await _delete_qdrant_points_for_article(article_id=article.id)


@pytest.mark.integration
async def test_index_article_updates_existing_chunks_when_text_changes(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bumping the article to a NEW ArticleVersion indexes a new set of chunks.

    The OLD version's chunks must remain in the DB and Qdrant (they're
    a historical record); the NEW version gets its own fresh chunks +
    points.
    """
    await _ensure_collection_ready()
    _patch_embed(monkeypatch, dim=DEFAULT_VECTOR_SIZE)
    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id)
    article = await _make_article(tenant_id=tenant.id, knowledge_base_id=kb.id)

    short_text = " ".join(f"oldw{i}" for i in range(150))
    long_text = " ".join(f"neww{i}" for i in range(900))  # 2 chunks at 800/100
    v1 = await _make_version(article_id=article.id, raw_text=short_text, version_number=1)
    await _set_article_current_version(article_id=article.id, version_id=v1.id)

    try:
        first = await index_article(article_id=article.id)
        assert first.status == ArticleStatus.INDEXED
        assert first.chunks_indexed == 1

        # Now create v2 with longer text and point article at it.
        v2 = await _make_version(article_id=article.id, raw_text=long_text, version_number=2)
        await _set_article_current_version(article_id=article.id, version_id=v2.id)

        second = await index_article(article_id=article.id)
        assert second.status == ArticleStatus.INDEXED
        assert second.chunks_indexed == 2

        # Old version's chunks still in the DB (history).
        v1_chunks = await _list_chunks_for_version(v1.id)
        assert len(v1_chunks) == 1
        # New version's chunks present.
        v2_chunks = await _list_chunks_for_version(v2.id)
        assert len(v2_chunks) == 2
        assert [c.chunk_index for c in v2_chunks] == [0, 1]

        # Qdrant: points for BOTH versions exist (3 total).
        v1_records = await _qdrant_scroll_by_article_version(
            article_version_id=v1.id
        )
        v2_records = await _qdrant_scroll_by_article_version(
            article_version_id=v2.id
        )
        assert len(v1_records) == 1
        assert len(v2_records) == 2

        # Every Qdrant payload carries tenant_id.
        for r in v1_records + v2_records:
            assert (r.payload or {}).get("tenant_id") == tenant.id
    finally:
        await _delete_qdrant_points_for_article(article_id=article.id)


@pytest.mark.integration
async def test_index_article_failure_sets_failed_status(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Embedding failure -> Article.status=FAILED, error_message starts with class name.

    Critically: error_message MUST NOT contain the raw embedding
    exception text (which can carry SDK error bodies with sensitive
    details).
    """
    await _ensure_collection_ready()
    from llm_client.types import EmbeddingError

    _patch_embed_fail(
        monkeypatch,
        exc=EmbeddingError("super-secret-key sk-test-12345"),
    )
    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id)
    article = await _make_article(tenant_id=tenant.id, knowledge_base_id=kb.id)
    raw_text = " ".join(f"w{i}" for i in range(50))
    version = await _make_version(article_id=article.id, raw_text=raw_text)
    await _set_article_current_version(
        article_id=article.id, version_id=version.id
    )

    try:
        result = await index_article(article_id=article.id)
        assert result.status == ArticleStatus.FAILED
        assert result.chunks_indexed == 0

        article_after = await _get_article(article.id)
        assert article_after.status == ArticleStatus.FAILED
        assert article_after.error_message is not None
        # error_message starts with the class name (no raw exception text).
        assert article_after.error_message.startswith("EmbeddingError")
        # Must not contain the secret we seeded in the original exc.
        assert "sk-test-12345" not in (article_after.error_message or "")

        # No Chunk rows were persisted for the failed version.
        chunks = await _list_chunks_for_version(version.id)
        assert chunks == []
    finally:
        await _delete_qdrant_points_for_article(article_id=article.id)


@pytest.mark.integration
async def test_index_article_oversize_text_marks_failed(
    tenant_factory: Tenant,
) -> None:
    """raw_text exceeding MAX_CHUNK_TEXT_BYTES -> Article.status=FAILED."""
    from knowledge.chunker import MAX_CHUNK_TEXT_BYTES

    await _ensure_collection_ready()
    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id)
    article = await _make_article(tenant_id=tenant.id, knowledge_base_id=kb.id)
    # Build a string strictly larger than MAX_CHUNK_TEXT_BYTES.
    big_text = "x" * (MAX_CHUNK_TEXT_BYTES + 1)
    version = await _make_version(article_id=article.id, raw_text=big_text)
    await _set_article_current_version(
        article_id=article.id, version_id=version.id
    )

    try:
        result = await index_article(article_id=article.id)
        assert result.status == ArticleStatus.FAILED

        article_after = await _get_article(article.id)
        assert article_after.status == ArticleStatus.FAILED
        # chunker raises ValueError; class name is stored.
        assert article_after.error_message is not None
        assert article_after.error_message.startswith("ValueError")

        chunks = await _list_chunks_for_version(version.id)
        assert chunks == []
    finally:
        await _delete_qdrant_points_for_article(article_id=article.id)


@pytest.mark.integration
async def test_index_article_missing_version_marks_failed(
    tenant_factory: Tenant,
) -> None:
    """Article with current_version_id=NULL -> Article.status=FAILED with ValueError.

    Catches the ``current_version_id IS NULL`` branch explicitly so a
    future regression that crashes the worker (instead of failing
    cleanly) is loud.
    """
    await _ensure_collection_ready()
    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id)
    article = await _make_article(tenant_id=tenant.id, knowledge_base_id=kb.id)
    # Do NOT create a version or set current_version_id.

    try:
        result = await index_article(article_id=article.id)
        assert result.status == ArticleStatus.FAILED

        article_after = await _get_article(article.id)
        assert article_after.status == ArticleStatus.FAILED
        assert article_after.error_message is not None
        assert article_after.error_message.startswith("ValueError")
    finally:
        await _delete_qdrant_points_for_article(article_id=article.id)


@pytest.mark.integration
async def test_qdrant_upsert_failure_marks_failed(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If upsert_chunks returns 0, the article goes to FAILED with no Chunk rows."""
    await _ensure_collection_ready()
    _patch_embed(monkeypatch, dim=DEFAULT_VECTOR_SIZE)
    _patch_qdrant_upsert_return_zero(monkeypatch)
    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id)
    article = await _make_article(tenant_id=tenant.id, knowledge_base_id=kb.id)
    raw_text = " ".join(f"w{i}" for i in range(50))
    version = await _make_version(article_id=article.id, raw_text=raw_text)
    await _set_article_current_version(
        article_id=article.id, version_id=version.id
    )

    try:
        result = await index_article(article_id=article.id)
        assert result.status == ArticleStatus.FAILED

        article_after = await _get_article(article.id)
        assert article_after.status == ArticleStatus.FAILED
        # The worker raises RuntimeError when upsert returns 0.
        assert article_after.error_message is not None
        assert article_after.error_message.startswith("RuntimeError")

        # No chunks were persisted (we never reached step 8).
        chunks = await _list_chunks_for_version(version.id)
        assert chunks == []
    finally:
        await _delete_qdrant_points_for_article(article_id=article.id)


@pytest.mark.integration
async def test_index_article_point_id_is_deterministic_from_chunk_id(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chunk.id + qdrant_point_id are deterministic across re-runs.

    ``Chunk.id`` is derived from (article_version_id, chunk_index) via
    a stable hash (see :func:`knowledge.worker._chunk_id`); the
    Qdrant point ID is a UUID5 of the chunk ID
    (:func:`knowledge.worker._qdrant_point_id`). The combination is
    what makes the worker idempotent at both the DB and Qdrant
    layers.

    We assert the exact derivation here so a future refactor that
    switches to a different hash / namespace (and silently re-maps
    every existing point) is flagged by the test.
    """
    await _ensure_collection_ready()
    _patch_embed(monkeypatch, dim=DEFAULT_VECTOR_SIZE)
    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id)
    article = await _make_article(tenant_id=tenant.id, knowledge_base_id=kb.id)
    raw_text = " ".join(f"w{i}" for i in range(50))
    version = await _make_version(article_id=article.id, raw_text=raw_text)
    await _set_article_current_version(
        article_id=article.id, version_id=version.id
    )

    try:
        from knowledge.worker import _chunk_id, _qdrant_point_id

        first = await index_article(article_id=article.id)
        assert first.status == ArticleStatus.INDEXED

        chunks = await _list_chunks_for_version(version.id)
        assert len(chunks) == 1
        chunk = chunks[0]
        # Determinism: chunk.id == _chunk_id(version_id, chunk_index).
        assert chunk.id == _chunk_id(version.id, chunk.chunk_index)
        # chunk.id fits the 26-char ULID column.
        assert len(chunk.id) == 26
        # qdrant_point_id == UUID5(_POINT_ID_NAMESPACE, f"chunk:{chunk_id}")
        assert chunk.qdrant_point_id == _qdrant_point_id(chunk.id)
        # UUID5 hex with hyphens is exactly 36 chars; Qdrant 1.13
        # only accepts unsigned int or UUID, so this MUST be a UUID.
        assert len(chunk.qdrant_point_id) == 36

        records = await _qdrant_scroll_by_article(article_id=article.id)
        assert len(records) == 1
        assert records[0].id == chunk.qdrant_point_id

        # Re-run: same point_id + chunk.id (the whole point of the contract).
        second = await index_article(article_id=article.id)
        assert second.status == ArticleStatus.INDEXED
        chunks_again = await _list_chunks_for_version(version.id)
        assert chunks_again[0].id == chunk.id
        assert chunks_again[0].qdrant_point_id == chunk.qdrant_point_id
    finally:
        await _delete_qdrant_points_for_article(article_id=article.id)
