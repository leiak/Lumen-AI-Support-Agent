"""Live-DB + Qdrant integration tests for ``RAGService.retrieve``.

Stage 12 / Task 4 — pins the new structured retrieval surface that the
``search_internal_kb`` LangChain tool depends on. Unlike
``RAGService.build_context_for_query`` (which returns a formatted
markdown block for prompt injection), ``RAGService.retrieve`` returns
a list of structured chunk snippets — each carrying ``chunk_id``,
``article_id``, ``article_title``, ``content``, ``score`` — so the
tool can format them as Markdown for the LLM.

PII discipline
--------------

Every article carries a distinctive ``MAGIC_PHRASE_*`` marker so
assertions never read real customer text. Logs use opaque IDs +
counts only (per the M1 PII contract).
"""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import pytest
from qdrant_client.http import models as qmodels

from channel.repository import ChannelRepository
from channel.enums import ChannelStatus, ChannelType
from core.database import get_session
from core.id_gen import new_id
from knowledge.enums import ArticleSourceType, ArticleStatus
from knowledge.models import (
    Article,
    ArticleVersion,
    KnowledgeBase,
)
from knowledge.qdrant_client import (
    DEFAULT_COLLECTION,
    DEFAULT_VECTOR_SIZE,
    ensure_collection,
)
from knowledge.rag_service import RAGService
from knowledge.repository import KnowledgeBaseRepository
from knowledge.worker import index_article
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository


# ============================================================================
# Fixtures / helpers
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_db_singletons() -> None:
    """Reset DB / Qdrant singletons between tests for isolation."""
    from core.database import reset_engine, reset_sessionmaker
    from core.qdrant import reset_qdrant_client

    reset_engine()
    reset_sessionmaker()
    reset_qdrant_client()
    yield
    reset_engine()
    reset_sessionmaker()
    reset_qdrant_client()


def _make_topic_vector(text: str, *, dim: int = DEFAULT_VECTOR_SIZE) -> list[float]:
    """Build a deterministic unit vector from the SHA-256 of ``text``."""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    expanded = (seed * ((dim // len(seed)) + 1))[:dim]
    raw = [(b / 127.5) - 1.0 for b in expanded]
    norm_sq = sum(x * x for x in raw) or 1.0
    norm = norm_sq ** 0.5
    return [x / norm for x in raw]


def _patch_embed_topic_coded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``embed_texts`` with deterministic topic-coded stubs."""

    async def _stub(
        *,
        texts: list[str],
        model: str,
        tenant_id: str | None = None,
        client: object = None,
    ) -> object:
        vectors = [_make_topic_vector(t) for t in texts]

        class _Res:
            def __init__(self, vs: list[list[float]]) -> None:
                self.vectors = vs
                self.model = "text-embedding-3-small"
                self.usage = type(
                    "U", (), {"prompt_tokens": 0, "total_tokens": 0}
                )()

        return _Res(vectors)

    from knowledge import retriever as retriever_module
    from knowledge import worker as worker_module

    monkeypatch.setattr(worker_module, "embed_texts", _stub)
    monkeypatch.setattr(retriever_module, "embed_texts", _stub)


async def _ensure_collection_ready() -> None:
    ok = await ensure_collection(
        name=DEFAULT_COLLECTION,
        vector_size=DEFAULT_VECTOR_SIZE,
    )
    assert ok, "Failed to ensure Qdrant collection"


async def _delete_tenant(tenant_id: str) -> None:
    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t:
            await session.delete(t)
            await session.commit()


async def _delete_qdrant_points_for_article(*, article_id: str) -> None:
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
    except Exception:  # best-effort
        pass


async def _make_kb(
    *,
    tenant_id: str,
    slug: str,
    name: str = "KB",
) -> KnowledgeBase:
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
    return kb


async def _make_article_and_version(
    *,
    tenant_id: str,
    knowledge_base_id: str,
    title: str,
    raw_text: str,
) -> tuple[Article, ArticleVersion]:
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


# Distinctive markers so cross-tenant tests can identify which article
# leaked (and the assertions can target article text without reading
# real customer strings).
_MAGIC_A = "MAGIC_PHRASE_RETRIEVE_ARTICLE_A"
_MAGIC_B = "MAGIC_PHRASE_RETRIEVE_ARTICLE_B"

_TEXT_A = (
    f"{_MAGIC_A}. This article explains the password reset flow used by "
    "the customer service widget. Customers open Settings, choose Security, "
    "and click the reset link sent to their registered email address."
)
_TEXT_B = (
    f"{_MAGIC_B}. This article explains international shipping rates for "
    "the retail platform. Standard international shipping takes 7 to 14 "
    "business days depending on destination and customs processing time."
)


# ============================================================================
# Tests
# ============================================================================


@pytest.mark.integration
async def test_retrieve_returns_top_k_chunks_for_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index 2 articles -> retrieve returns at least 1 snippet with the
    expected fields and tenant-scoped data.

    Pins the new ``retrieve()`` contract: ``chunk_id``,
    ``article_id``, ``article_title``, ``content``, ``score`` must all
    be present on every returned snippet, and ``tenant_id`` must be
    threaded into the underlying Qdrant filter.
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)

    tenant = await TenantRepository().create(
        name="Retrieve Top K Tenant", plan=TenantPlan.FREE
    )
    kb = await _make_kb(tenant_id=tenant.id, slug="retrieve-kb")
    article_a, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Password Reset",
        raw_text=_TEXT_A,
    )
    article_b, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Shipping Policy",
        raw_text=_TEXT_B,
    )
    try:
        for art in (article_a, article_b):
            result = await index_article(article_id=art.id)
            assert result.status == ArticleStatus.INDEXED

        rag = RAGService()
        # Use a topic-coded query so at least one of the indexed
        # chunks returns a meaningful similarity score (above the
        # default threshold).
        snippets = await rag.retrieve(
            tenant_id=tenant.id,
            query="reset password",
            kb_slug=kb.slug,
            top_k=3,
            score_threshold=None,  # disable threshold for mock embeddings
        )

        assert isinstance(snippets, list)
        assert len(snippets) >= 1
        # Returned snippets are score-sorted descending.
        scores = [s["score"] for s in snippets]
        assert scores == sorted(scores, reverse=True)
        # At least one snippet carries the requested article's marker.
        assert any(_MAGIC_A in s["content"] for s in snippets), (
            f"expected {_MAGIC_A} marker in retrieved content; got "
            f"{[s['content'][:60] for s in snippets]!r}"
        )
        # Every snippet has the documented contract fields.
        for s in snippets:
            assert isinstance(s["chunk_id"], str) and s["chunk_id"]
            assert isinstance(s["article_id"], str) and s["article_id"]
            assert isinstance(s["article_title"], str) and s["article_title"]
            assert isinstance(s["content"], str)
            assert isinstance(s["score"], float)
    finally:
        for art in (article_a, article_b):
            await _delete_qdrant_points_for_article(article_id=art.id)
        await _delete_tenant(tenant.id)


@pytest.mark.integration
async def test_retrieve_returns_empty_list_when_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bogus KB slug for the tenant returns ``[]`` — never raises, never
    returns ``None``. The tool relies on this graceful empty path so
    the LLM sees a "No relevant articles found" message rather than
    a hard tool error.
    """
    _patch_embed_topic_coded(monkeypatch)
    tenant = await TenantRepository().create(
        name="Retrieve Empty Tenant", plan=TenantPlan.FREE
    )
    try:
        rag = RAGService()
        snippets = await rag.retrieve(
            tenant_id=tenant.id,
            query="anything",
            kb_slug="nonexistent-slug",
            top_k=5,
        )
        assert snippets == []
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
async def test_retrieve_filters_by_kb_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two KBs, two distinct articles -> ``retrieve(kb_slug='kb-a')``
    MUST only return chunks from KB-A, never from KB-B. Pins the
    slug filter contract.
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)

    tenant = await TenantRepository().create(
        name="Retrieve KB-Slug Tenant", plan=TenantPlan.FREE
    )
    kb_a = await _make_kb(tenant_id=tenant.id, slug="kb-a", name="KB A")
    kb_b = await _make_kb(tenant_id=tenant.id, slug="kb-b", name="KB B")
    article_a, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb_a.id,
        title="KB A Article",
        raw_text=_TEXT_A,
    )
    article_b, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb_b.id,
        title="KB B Article",
        raw_text=_TEXT_B,
    )
    try:
        for art in (article_a, article_b):
            result = await index_article(article_id=art.id)
            assert result.status == ArticleStatus.INDEXED

        rag = RAGService()
        snippets = await rag.retrieve(
            tenant_id=tenant.id,
            query="reset password",
            kb_slug="kb-a",
            top_k=5,
            score_threshold=None,
        )
        # Each returned snippet's article_id MUST belong to KB-A.
        for s in snippets:
            assert s["article_id"] in {article_a.id}, (
                f"kb_slug filter leaked: snippet {s!r} belongs to a "
                f"different KB"
            )
        # And the KB-B marker MUST NOT appear in any snippet content.
        all_content = "\n".join(s["content"] for s in snippets)
        assert _MAGIC_B not in all_content
    finally:
        for art in (article_a, article_b):
            await _delete_qdrant_points_for_article(article_id=art.id)
        await _delete_tenant(tenant.id)


@pytest.mark.integration
async def test_retrieve_cross_tenant_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant A's KB content MUST NOT appear when tenant B queries.

    Pin the multi-tenant isolation contract: a retrieve call from
    tenant B with a slug only-registered under tenant A returns
    ``[]`` (slug is invisible cross-tenant), and even without a slug
    the Qdrant ``tenant_id`` payload filter keeps chunks scoped.
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)

    tenant_a = await TenantRepository().create(
        name="Retrieve X-Tenant A", plan=TenantPlan.FREE
    )
    tenant_b = await TenantRepository().create(
        name="Retrieve X-Tenant B", plan=TenantPlan.FREE
    )
    kb_a = await _make_kb(tenant_id=tenant_a.id, slug="shared-slug")
    article_a, _ = await _make_article_and_version(
        tenant_id=tenant_a.id,
        knowledge_base_id=kb_a.id,
        title="Tenant A Article",
        raw_text=_TEXT_A,
    )
    try:
        result = await index_article(article_id=article_a.id)
        assert result.status == ArticleStatus.INDEXED

        # Sanity: tenant A can see its own slug.
        repo = KnowledgeBaseRepository()
        a_visible = await repo.find_by_slug(
            tenant_id=tenant_a.id, slug="shared-slug"
        )
        assert a_visible is not None

        rag = RAGService()

        # Tenant B queries with the slug it does NOT own -> empty.
        b_snippets = await rag.retrieve(
            tenant_id=tenant_b.id,
            query="reset password",
            kb_slug="shared-slug",
            top_k=5,
        )
        assert b_snippets == [], (
            f"cross-tenant slug filter leaked: tenant B saw "
            f"{len(b_snippets)} snippets from tenant A's KB"
        )

        # Tenant B queries without a slug -> tenant A's KB still
        # invisible (no KBs for tenant B at all -> empty).
        b_no_slug = await rag.retrieve(
            tenant_id=tenant_b.id,
            query="reset password",
            top_k=5,
        )
        assert b_no_slug == [], (
            f"cross-tenant no-slug query leaked: tenant B saw "
            f"{len(b_no_slug)} snippets"
        )
    finally:
        await _delete_qdrant_points_for_article(article_id=article_a.id)
        await _delete_tenant(tenant_a.id)
        await _delete_tenant(tenant_b.id)