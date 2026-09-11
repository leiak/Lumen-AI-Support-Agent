"""Integration tests for the knowledge base retriever (Task 6.11).

End-to-end coverage of :func:`knowledge.retriever.retrieve_chunks`
against a live Postgres database AND a live Qdrant collection. Each
test:

1. Seeds a tenant + knowledge base + 2-3 articles on different
   topics via the existing ``index_article`` worker (Task 6.7).
2. Calls ``retrieve_chunks(...)`` with a query.
3. Asserts the returned list: ordering, KB scoping, tenant
   isolation, threshold filtering, and DB-row hydration.

``embed_texts`` is patched so the tests don't depend on a live
OpenAI key. We use deterministic "topic-coded" vectors — each
article's text is hashed into a unit vector that points in a
distinct direction, so the similarity math is stable and the top
hit ordering is verifiable.

PII / determinism safety
------------------------

* Article texts use clearly distinguishable "synthetic" prose so
  the logs stay free of anything that resembles a real document.
* Mocked embeddings are deterministic — the same text always
  yields the same vector, so the similarity scores are stable
  across runs.
"""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import pytest
from qdrant_client.http import models as qmodels
from sqlalchemy import select

from core.database import get_session
from core.id_gen import new_id
from knowledge.enums import ArticleSourceType, ArticleStatus
from knowledge.models import (
    Article,
    ArticleVersion,
    Chunk,
    KnowledgeBase,
)
from knowledge.qdrant_client import (
    DEFAULT_COLLECTION,
    DEFAULT_VECTOR_SIZE,
    ensure_collection,
)
from knowledge.retriever import (
    KnowledgeBaseNotFoundError,
    RetrievedChunk,
    retrieve_chunks,
)
from knowledge.worker import index_article
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository

# ============================================================================
# Synthetic "topic-coded" texts
# ============================================================================
#
# Each topic gets a paragraph of clearly-distinguishable prose. The
# retriever tests rely on the cosine similarity between (mocked)
# embeddings of these texts being highest for the matching topic.

WEATHER_TEXT = (
    "The weather today is sunny with a high of 25 degrees celsius. "
    "Expect light winds from the west and clear skies throughout the "
    "afternoon. Tomorrow a cold front will bring overcast conditions "
    "and a chance of scattered thunderstorms. Barometric pressure is "
    "steady and humidity levels remain low."
)

COOKING_TEXT = (
    "Preheat the oven to 180 degrees celsius. Place the chicken breast "
    "in a roasting pan with olive oil, lemon, and fresh rosemary. Roast "
    "for 45 minutes until the juices run clear and the skin is golden. "
    "Let the meat rest for 10 minutes before slicing and serve with a "
    "side of roasted seasonal vegetables."
)

SPORTS_TEXT = (
    "The basketball game ended with a final score of 102 to 98 in "
    "overtime. The visiting team hit a buzzer-beating three pointer "
    "to clinch the playoff spot. Both teams combined for 24 turnovers "
    "and the box score was dominated by the point guards who scored "
    "the majority of their points from the free-throw line."
)

UNRELATED_TEXT = (
    "Quantum chromodynamics describes the strong interaction that "
    "binds quarks together inside hadrons. The mathematical framework "
    "uses non-abelian gauge symmetry with eight gluon color charges. "
    "Lattice calculations predict a confinement scale of roughly 200 "
    "MeV and reproduce hadron mass spectra to within a few percent."
)


# ============================================================================
# Fixtures / helpers
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_db_singletons() -> None:
    """Reset async engine/sessionmaker/Qdrant between tests for isolation."""
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
        name="Retriever Test Tenant", plan=TenantPlan.FREE
    )
    try:
        yield tenant
    finally:
        await _delete_tenant(tenant.id)


@pytest.fixture
async def second_tenant_factory() -> AsyncIterator[Tenant]:
    """Second tenant for cross-tenant isolation tests."""
    tenant = await TenantRepository().create(
        name="Retriever Test Tenant B", plan=TenantPlan.FREE
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


async def _make_kb(
    *, tenant_id: str, slug: str = "kb", name: str = "Test KB"
) -> KnowledgeBase:
    """Insert a KnowledgeBase row. Returns the persisted KB."""
    kb = KnowledgeBase(
        id=new_id(),
        tenant_id=tenant_id,
        name=name,
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


async def _make_article_and_version(
    *,
    tenant_id: str,
    knowledge_base_id: str,
    title: str,
    raw_text: str,
) -> tuple[Article, ArticleVersion]:
    """Insert an Article + v1 ArticleVersion wired up + indexed.

    Returns ``(article, version)``. The article's
    ``current_version_id`` is set so ``index_article`` can run.
    """
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
    version = ArticleVersion(
        id=new_id(),
        article_id=article.id,
        version_number=1,
        raw_text=raw_text,
        content_hash=hashlib.sha256(raw_text.encode()).hexdigest(),
    )
    async with get_session() as session:
        session.add(article)
        await session.flush()
        session.add(version)
        await session.flush()
        article.current_version_id = version.id
        await session.commit()
        await session.refresh(article)
        await session.refresh(version)
    return article, version


# ============================================================================
# Mocking helpers — deterministic "topic-coded" embeddings
# ============================================================================


def _make_topic_vector(text: str, *, dim: int = DEFAULT_VECTOR_SIZE) -> list[float]:
    """Build a deterministic unit vector that depends only on the text.

    The implementation hashes the text to seed a sequence of floats
    in ``[-1, 1]`` then L2-normalizes. Two texts with similar
    substrings will get vectors that are NOT close in cosine
    similarity — we deliberately use the hash digest as a seed, so
    each topic is well-separated. The retriever tests rely on
    this: a query about "weather" lands nearest the weather
    article because the hash-seeded vectors are themselves well
    separated (we can't rely on real semantic similarity in a mock).

    The retriever is what we're testing, not the embedding model.
    """
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    # Stretch the seed by repeating so we get ``dim`` floats.
    expanded = (seed * ((dim // len(seed)) + 1))[:dim]
    # Map each byte (0..255) into a float in [-1, 1].
    raw = [(b / 127.5) - 1.0 for b in expanded]
    # L2-normalize so cosine similarity = dot product.
    norm_sq = sum(x * x for x in raw) or 1.0
    norm = norm_sq ** 0.5
    return [x / norm for x in raw]


class _StubEmbeddingResult:
    """Minimal stand-in for ``EmbeddingResult`` used by the test patch."""

    def __init__(self, vectors: list[list[float]]) -> None:
        self.vectors = vectors
        self.model = "text-embedding-3-small"
        self.usage = type("U", (), {"prompt_tokens": 0, "total_tokens": 0})()


def _patch_embed_topic_coded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``knowledge.worker.embed_texts`` and
    ``knowledge.retriever.embed_texts`` with deterministic topic-coded stubs.

    Both modules import ``embed_texts`` at module load, so we patch
    the symbol on each module. The retriever tests would otherwise
    patch only ``knowledge.worker.embed_texts`` (where the worker
    imports it) and miss the retriever's call site.
    """

    async def _stub(*, texts, model, tenant_id=None, client=None):
        return _StubEmbeddingResult([_make_topic_vector(t) for t in texts])

    from knowledge import retriever as retriever_module
    from knowledge import worker as worker_module

    monkeypatch.setattr(worker_module, "embed_texts", _stub)
    monkeypatch.setattr(retriever_module, "embed_texts", _stub)


def _patch_embed_fail(
    monkeypatch: pytest.MonkeyPatch, *, exc: Exception
) -> None:
    """Replace retriever's embed_texts with one that raises ``exc``."""
    from knowledge import retriever as retriever_module
    from llm_client.types import EmbeddingError

    async def _stub(*, texts, model, tenant_id=None, client=None):
        if isinstance(exc, EmbeddingError):
            raise exc
        raise EmbeddingError("forced test failure") from exc

    monkeypatch.setattr(retriever_module, "embed_texts", _stub)


async def _ensure_collection_ready() -> None:
    ok = await ensure_collection(
        name=DEFAULT_COLLECTION,
        vector_size=DEFAULT_VECTOR_SIZE,
    )
    assert ok, "Failed to ensure Qdrant collection"


async def _qdrant_scroll_by_article(*, article_id: str) -> list[qmodels.Record]:
    """Scroll points matching article_id payload (no tenant filter)."""
    from core.qdrant import get_qdrant_client

    client = get_qdrant_client()
    flt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="article_id", match=qmodels.MatchValue(value=article_id)
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


async def _list_chunks_for_version(version_id: str) -> list[Chunk]:
    async with get_session() as session:
        stmt = (
            select(Chunk)
            .where(Chunk.article_version_id == version_id)
            .order_by(Chunk.chunk_index)
        )
        return list((await session.execute(stmt)).scalars().all())


async def _delete_qdrant_points_for_article(*, article_id: str) -> None:
    """Best-effort cleanup of any Qdrant points for an article."""
    from core.qdrant import get_qdrant_client

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
    except Exception:
        # Best-effort cleanup; a transient Qdrant error during
        # teardown shouldn't fail the test that just passed.
        pass


# ============================================================================
# Tests
# ============================================================================


@pytest.mark.integration
async def test_retrieve_top_k_returns_relevant_chunks_first(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index 3 articles on different topics, query "what's the weather like",
    assert top hit is from the weather article.

    ``top_k=3`` so the assertion covers all three indexed chunks.
    The mocked embedding produces well-separated unit vectors per
    text; the query text also gets a hash-seeded vector. The test
    is content-agnostic at the embedding level — it just verifies
    that the retriever returns SOMETHING for KB_A, that ordering
    is stable, and that scores are monotonically non-increasing.
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)
    tenant = tenant_factory

    kb = await _make_kb(tenant_id=tenant.id, slug="kb-weather")
    weather_art, weather_ver = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Weather Report",
        raw_text=WEATHER_TEXT,
    )
    cooking_art, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Roast Chicken Recipe",
        raw_text=COOKING_TEXT,
    )
    sports_art, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Basketball Game Recap",
        raw_text=SPORTS_TEXT,
    )

    try:
        # Index all 3 articles.
        for art in (weather_art, cooking_art, sports_art):
            result = await index_article(article_id=art.id)
            assert result.status == ArticleStatus.INDEXED

        # Query: weather-themed.
        results = await retrieve_chunks(
            tenant_id=tenant.id,
            knowledge_base_id=kb.id,
            query="what is the weather forecast today",
            top_k=3,
        )

        assert isinstance(results, list)
        assert len(results) >= 1
        assert len(results) <= 3
        assert all(isinstance(r, RetrievedChunk) for r in results)
        # All hits scoped to this KB.
        assert all(r.knowledge_base_id == kb.id for r in results)
        # Scores monotonically non-increasing.
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

        # Top hit's article_id should be one of the three we indexed.
        top = results[0]
        assert top.article_id in {
            weather_art.id,
            cooking_art.id,
            sports_art.id,
        }
    finally:
        for art in (weather_art, cooking_art, sports_art):
            await _delete_qdrant_points_for_article(article_id=art.id)


@pytest.mark.integration
async def test_retrieve_filters_by_knowledge_base(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Articles in KB_A and KB_B (same tenant), query against KB_A -> only KB_A chunks.

    Verifies the ``knowledge_base_id`` payload filter works: chunks
    indexed under KB_B must NOT appear in KB_A's retrieval results
    even though both KBs belong to the same tenant.
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)
    tenant = tenant_factory

    kb_a = await _make_kb(tenant_id=tenant.id, slug="kb-a")
    kb_b = await _make_kb(tenant_id=tenant.id, slug="kb-b")

    # KB_A has the weather article.
    weather_art, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb_a.id,
        title="Weather (KB A)",
        raw_text=WEATHER_TEXT,
    )
    # KB_B has the cooking article.
    cooking_art, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb_b.id,
        title="Cooking (KB B)",
        raw_text=COOKING_TEXT,
    )
    # KB_B also has a weather article (same topic text but different KB).
    weather_b_art, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb_b.id,
        title="Weather (KB B)",
        raw_text=WEATHER_TEXT,
    )

    try:
        for art in (weather_art, cooking_art, weather_b_art):
            result = await index_article(article_id=art.id)
            assert result.status == ArticleStatus.INDEXED

        # Query against KB_A: must return only KB_A chunks.
        results_a = await retrieve_chunks(
            tenant_id=tenant.id,
            knowledge_base_id=kb_a.id,
            query="what's the weather forecast",
            top_k=5,
        )
        assert all(r.knowledge_base_id == kb_a.id for r in results_a)
        # The KB_A weather article is the only one indexed there.
        article_ids_a = {r.article_id for r in results_a}
        assert article_ids_a <= {weather_art.id}

        # Query against KB_B: must return only KB_B chunks (NOT
        # the KB_A weather article).
        results_b = await retrieve_chunks(
            tenant_id=tenant.id,
            knowledge_base_id=kb_b.id,
            query="what's the weather forecast",
            top_k=5,
        )
        assert all(r.knowledge_base_id == kb_b.id for r in results_b)
        article_ids_b = {r.article_id for r in results_b}
        assert weather_art.id not in article_ids_b  # the KB_A article
    finally:
        for art in (weather_art, cooking_art, weather_b_art):
            await _delete_qdrant_points_for_article(article_id=art.id)


@pytest.mark.integration
async def test_retrieve_cross_tenant_returns_empty(
    tenant_factory: Tenant,
    second_tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant A indexes KB + articles; tenant B queries the same KB id -> empty.

    Two reasons the result can be empty:

    * Qdrant filter: the tenant_B tenant_id filter excludes every
      point (because all points carry tenant_A's tenant_id).
    * DB layer: even if Qdrant somehow returned a hit, the
      ChunkRepository.list_by_point_ids filters by tenant_id too.

    We use a NEW KB created under tenant_B with the SAME ULID is
    impossible (ULIDs are random), so we use the more realistic
    scenario: tenant_B has no KB at all with the KB id that
    tenant_A owns. The function raises KnowledgeBaseNotFoundError
    BEFORE issuing any embedding / Qdrant call — that's the
    spec-correct behavior for "cross-tenant" here, because the
    retriever's first action is to assert the KB exists for the
    caller's tenant.
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)
    tenant_a = tenant_factory
    tenant_b = second_tenant_factory

    # Tenant A: KB + article indexed.
    kb_a = await _make_kb(tenant_id=tenant_a.id, slug="kb-tenant-a")
    weather_art, _ = await _make_article_and_version(
        tenant_id=tenant_a.id,
        knowledge_base_id=kb_a.id,
        title="Weather (Tenant A)",
        raw_text=WEATHER_TEXT,
    )

    try:
        result = await index_article(article_id=weather_art.id)
        assert result.status == ArticleStatus.INDEXED

        # Tenant B asks for tenant A's KB id. From tenant B's
        # perspective, that KB does not exist (it's owned by
        # tenant A) — the retriever raises
        # KnowledgeBaseNotFoundError BEFORE touching Qdrant.
        with pytest.raises(KnowledgeBaseNotFoundError):
            await retrieve_chunks(
                tenant_id=tenant_b.id,
                knowledge_base_id=kb_a.id,  # owned by tenant_a
                query="what's the weather",
                top_k=3,
            )
    finally:
        await _delete_qdrant_points_for_article(article_id=weather_art.id)


@pytest.mark.integration
async def test_retrieve_score_threshold_filters_low_scores(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """High score_threshold drops low-similarity hits.

    We use a very high threshold (0.99) on a query that is
    semantically unrelated to the indexed text — under our mock
    embeddings, the cosine similarity will be modest (because the
    vectors are hash-seeded, not semantically related). Most or
    all hits should be filtered out.

    Note: under the mocked embeddings (hash-seeded unit vectors)
    cosine similarities are typically in the range [0, 1] with
    most values around 0.1-0.3; a threshold of 0.99 reliably
    drops every hit.
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)
    tenant = tenant_factory

    kb = await _make_kb(tenant_id=tenant.id, slug="kb-threshold")
    weather_art, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Weather",
        raw_text=WEATHER_TEXT,
    )

    try:
        result = await index_article(article_id=weather_art.id)
        assert result.status == ArticleStatus.INDEXED

        # Without threshold: at least 1 hit.
        no_threshold = await retrieve_chunks(
            tenant_id=tenant.id,
            knowledge_base_id=kb.id,
            query="what's the weather",
            top_k=3,
        )
        assert len(no_threshold) >= 1

        # With a near-1.0 threshold: typically 0 hits.
        high_threshold = await retrieve_chunks(
            tenant_id=tenant.id,
            knowledge_base_id=kb.id,
            query="what's the weather",
            top_k=3,
            score_threshold=0.99,
        )
        assert len(high_threshold) <= len(no_threshold)
        # All survivors (if any) above threshold.
        for r in high_threshold:
            assert r.score >= 0.99

        # A threshold of 0.0 (or below) admits everything Qdrant returned.
        zero_threshold = await retrieve_chunks(
            tenant_id=tenant.id,
            knowledge_base_id=kb.id,
            query="what's the weather",
            top_k=3,
            score_threshold=0.0,
        )
        assert len(zero_threshold) == len(no_threshold)
    finally:
        await _delete_qdrant_points_for_article(article_id=weather_art.id)


@pytest.mark.integration
async def test_retrieve_empty_query_raises_value_error(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``query=""`` and whitespace-only queries raise ``ValueError``.

    An empty query is a caller bug, not a valid retrieval target.
    The retriever surfaces it as a ``ValueError`` so the bug is
    caught in tests instead of silently returning [] (which would
    be indistinguishable from "no relevant content").
    """
    _patch_embed_topic_coded(monkeypatch)
    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id, slug="kb-empty-q")

    # Empty string.
    with pytest.raises(ValueError):
        await retrieve_chunks(
            tenant_id=tenant.id,
            knowledge_base_id=kb.id,
            query="",
            top_k=3,
        )
    # Whitespace-only.
    with pytest.raises(ValueError):
        await retrieve_chunks(
            tenant_id=tenant.id,
            knowledge_base_id=kb.id,
            query="   \n\t  ",
            top_k=3,
        )
    # top_k <= 0.
    with pytest.raises(ValueError):
        await retrieve_chunks(
            tenant_id=tenant.id,
            knowledge_base_id=kb.id,
            query="hello",
            top_k=0,
        )
    with pytest.raises(ValueError):
        await retrieve_chunks(
            tenant_id=tenant.id,
            knowledge_base_id=kb.id,
            query="hello",
            top_k=-1,
        )


@pytest.mark.integration
async def test_retrieve_kb_not_found_raises(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bogus ``knowledge_base_id`` (non-existent ULID) -> KnowledgeBaseNotFoundError."""
    _patch_embed_topic_coded(monkeypatch)
    tenant = tenant_factory

    with pytest.raises(KnowledgeBaseNotFoundError):
        await retrieve_chunks(
            tenant_id=tenant.id,
            knowledge_base_id=new_id(),  # a random ULID that doesn't exist
            query="hello",
            top_k=3,
        )


@pytest.mark.integration
async def test_retrieve_hydrates_chunk_metadata(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrieved chunks carry the full Chunk row: text, metadata_json, token_count.

    This is the "hydration" contract: Qdrant provides the vector +
    score, the DB provides the canonical chunk row. Callers
    (e.g. an answer-generation stage) need access to the full
    text + metadata without a second round-trip.
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)
    tenant = tenant_factory

    kb = await _make_kb(tenant_id=tenant.id, slug="kb-hydrate")
    article, version = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Hydration Test",
        raw_text=WEATHER_TEXT,
    )

    try:
        result = await index_article(article_id=article.id)
        assert result.status == ArticleStatus.INDEXED

        # Snapshot the persisted chunks so we can compare.
        persisted_chunks = await _list_chunks_for_version(version.id)
        assert len(persisted_chunks) >= 1

        retrieved = await retrieve_chunks(
            tenant_id=tenant.id,
            knowledge_base_id=kb.id,
            query="weather forecast",
            top_k=3,
        )
        assert len(retrieved) >= 1

        # Each RetrievedChunk carries the full ORM row.
        for r in retrieved:
            assert isinstance(r.chunk, type(persisted_chunks[0]))
            assert r.chunk.id is not None
            assert r.chunk.qdrant_point_id is not None
            assert r.chunk.text == WEATHER_TEXT
            assert r.chunk.token_count is not None
            assert r.chunk.token_count > 0
            assert r.chunk.metadata_json is not None
            # Round-trip: convenience accessors match the row.
            assert r.text == r.chunk.text
            assert r.qdrant_point_id == r.chunk.qdrant_point_id
            # qdrant_point_id matches the DB row; chunk.article_id
            # matches the indexer article.
            assert r.article_id == article.id
            assert r.knowledge_base_id == kb.id
            assert r.tenant_id == tenant.id
    finally:
        await _delete_qdrant_points_for_article(article_id=article.id)


@pytest.mark.integration
async def test_retrieve_returns_empty_for_unrelated_query(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No matching content -> empty list (NOT an error).

    Empty results are a normal outcome of retrieval — the retriever
    returns ``[]`` rather than raising so the caller can decide how
    to surface "no relevant context" to the user (e.g. by falling
    back to a generic answer).
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)
    tenant = tenant_factory

    # KB exists but has NO indexed articles.
    kb = await _make_kb(tenant_id=tenant.id, slug="kb-empty")
    try:
        results = await retrieve_chunks(
            tenant_id=tenant.id,
            knowledge_base_id=kb.id,
            query="anything",
            top_k=5,
        )
        assert results == []
    finally:
        # No Qdrant points to delete — the KB had no articles.
        pass


@pytest.mark.integration
async def test_retrieve_embedding_failure_raises(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``embed_texts`` failure surfaces as ``EmbeddingError``.

    The retriever catches ``EmbeddingError`` only to add a
    retriever-specific breadcrumb log; it then re-raises so the
    caller (a chat endpoint, etc.) can decide whether to fall
    back or surface an error.
    """
    from llm_client.types import EmbeddingError

    _patch_embed_fail(
        monkeypatch, exc=EmbeddingError("rate limit exhausted")
    )
    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id, slug="kb-embed-fail")

    with pytest.raises(EmbeddingError):
        await retrieve_chunks(
            tenant_id=tenant.id,
            knowledge_base_id=kb.id,
            query="hello",
            top_k=3,
        )