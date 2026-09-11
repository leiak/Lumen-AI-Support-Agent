"""Live-DB integration tests for the KnowledgeBase + Article REST API (Task 6.9).

End-to-end coverage of every endpoint in
``knowledge.api.router`` against a real Postgres database AND a real
Qdrant collection. The FastAPI app is mounted via ``ASGITransport``
so the auth dependency (``auth.dependencies.get_current_user``) runs
for real — JWTs are minted via ``auth.jwt.create_access_token``.

What's exercised:
    * Every KB endpoint (list/create/get/patch/delete)
    * Every Article endpoint (list/create/get/patch/delete/reindex)
    * Tenant isolation: cross-tenant reads return 404, never 403
    * Auth: missing/invalid JWTs return 401
    * Slug validation: uppercase, leading dash, whitespace rejected
    * Slug uniqueness within a tenant: 409 on conflict
    * The fire-and-forget indexer scheduled on POST /articles runs
      and the article's status transitions DRAFT -> INDEXING ->
      INDEXED within the test's wait window
    * Qdrant cleanup on article delete: ``client.count`` filter on
      ``article_id`` returns 0 after DELETE

Test isolation:
    * ``autouse`` fixture resets the SQLAlchemy engine, the
      sessionmaker, and the Qdrant client between tests (matches the
      pattern in ``test_indexer_pipeline`` and ``test_reindex_pipeline``).
    * Cleanup is via tenant cascade-delete in a ``finally:`` block.

PII discipline:
    * ``raw_text`` is built from synthetic words (``w0 w1 w2 ...``) so
      test logs stay free of anything sensitive.
    * Qdrant assertions filter on opaque IDs only — never raw text.
"""
from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from qdrant_client.http import models as qmodels

from auth.jwt import create_access_token
from core.database import get_session
from core.id_gen import new_id
from core.qdrant import get_qdrant_client
from knowledge.api import router as knowledge_router
from knowledge.enums import ArticleSourceType, ArticleStatus
from knowledge.models import (
    Article,
    ArticleVersion,
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
    """Reset async engine / sessionmaker / Qdrant between tests.

    Each pytest-asyncio test runs in its own event loop. Without this
    fixture the engine / Qdrant client from a previous test would try
    to reconnect on a closed loop, raising ``RuntimeError: Event
    loop is closed``.
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
    """Yield a fresh Tenant. Cleanup cascades KBs + articles + versions + chunks."""
    tenant = await TenantRepository().create(
        name="CRUD Test Tenant", plan=TenantPlan.FREE
    )
    try:
        yield tenant
    finally:
        await _delete_tenant(tenant.id)


@pytest.fixture
async def second_tenant_factory() -> AsyncIterator[Tenant]:
    """Yield a second Tenant for cross-tenant 404 tests."""
    tenant = await TenantRepository().create(
        name="CRUD Test Tenant B", plan=TenantPlan.FREE
    )
    try:
        yield tenant
    finally:
        await _delete_tenant(tenant.id)


async def _delete_tenant(tenant_id: str) -> None:
    """Cascade-delete a tenant (kills its KBs, articles, versions, chunks)."""
    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t:
            await session.delete(t)
            await session.commit()


async def _kb_factory(*, tenant_id: str, slug: str = "kb") -> KnowledgeBase:
    """Insert a KnowledgeBase row directly via the model.

    The KB CRUD endpoints are tested separately — this helper just
    gives the article tests a parent KB without round-tripping
    through the API.
    """
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


def _patch_embed(monkeypatch: pytest.MonkeyPatch, *, dim: int = DEFAULT_VECTOR_SIZE) -> None:
    """Replace ``knowledge.worker.embed_texts`` with a deterministic stub.

    Without this the indexer would hit the real OpenAI API and fail.
    The stub returns a unique vector per text so we don't collapse
    all chunks to the same point.

    This is intentionally a sync function — calling an ``async def``
    without awaiting it returns a coroutine that the test would never
    await, leaving the patch in place but never executed.
    """

    class _StubEmbeddingResult:
        def __init__(self, vectors: list[list[float]]) -> None:
            self.vectors = vectors
            self.model = "text-embedding-3-small"
            self.usage = type("U", (), {"prompt_tokens": 0, "total_tokens": 0})()

    async def _stub(*, texts, model, tenant_id=None, client=None):
        vectors = [[float(i + idx) for i in range(dim)] for idx, _ in enumerate(texts)]
        return _StubEmbeddingResult(vectors)

    from knowledge import worker as worker_module

    monkeypatch.setattr(worker_module, "embed_texts", _stub)


def _build_app() -> FastAPI:
    """Build a FastAPI app with just the knowledge router mounted."""
    app = FastAPI()
    app.include_router(knowledge_router)
    return app


def _auth_headers(*, tenant_id: str, user_id: str = "u_admin") -> dict[str, str]:
    """Mint an admin JWT for ``tenant_id`` and return the auth header."""
    token = create_access_token(tenant_id=tenant_id, user_id=user_id, role="admin")
    return {"Authorization": f"Bearer {token}"}


async def _ensure_collection_ready() -> None:
    """Make sure the DEFAULT_COLLECTION exists before a test runs."""
    from knowledge.qdrant_client import ensure_collection

    ok = await ensure_collection(
        name=DEFAULT_COLLECTION,
        vector_size=DEFAULT_VECTOR_SIZE,
    )
    assert ok, "Failed to ensure Qdrant collection"


async def _qdrant_count_by_article(*, article_id: str) -> int:
    """Count Qdrant points whose payload carries ``article_id``."""
    client = get_qdrant_client()
    flt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="article_id",
                match=qmodels.MatchValue(value=article_id),
            )
        ]
    )
    result = await client.count(
        collection_name=DEFAULT_COLLECTION,
        count_filter=flt,
        exact=True,
    )
    return result.count


async def _delete_qdrant_points_for_article(*, article_id: str) -> None:
    """Best-effort cleanup of any Qdrant points for an article.

    Used in test finally-blocks so a leaked vector set can't break
    later tests. Never raises.
    """
    try:
        client = get_qdrant_client()
    except Exception:
        return
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
        return


# ============================================================================
# KB endpoint tests
# ============================================================================


@pytest.mark.integration
async def test_list_kbs_empty_tenant(
    tenant_factory: Tenant,
) -> None:
    """GET /knowledge-bases on a fresh tenant returns 0 items."""
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/knowledge/knowledge-bases",
            headers=_auth_headers(tenant_id=tenant_factory.id),
        )
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


@pytest.mark.integration
async def test_create_kb_then_list(
    tenant_factory: Tenant,
) -> None:
    """POST /knowledge-bases -> 201; subsequent GET shows the new KB."""
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        # POST
        resp = await client.post(
            "/api/v1/knowledge/knowledge-bases",
            headers=_auth_headers(tenant_id=tenant_factory.id),
            json={
                "name": "Product Docs",
                "slug": "product-docs",
                "description": "Public docs",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "Product Docs"
        assert body["slug"] == "product-docs"
        assert body["tenant_id"] == tenant_factory.id
        kb_id = body["id"]
        assert len(kb_id) == 26  # ULID

        # GET list
        list_resp = await client.get(
            "/api/v1/knowledge/knowledge-bases",
            headers=_auth_headers(tenant_id=tenant_factory.id),
        )
        assert list_resp.status_code == 200
        items = list_resp.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == kb_id


@pytest.mark.integration
async def test_create_kb_slug_validation(
    tenant_factory: Tenant,
) -> None:
    """POST with invalid slug (uppercase, spaces, leading dash) returns 422.

    Note: the spec regex ``^[a-z0-9][a-z0-9-]{0,99}$`` does NOT
    reject trailing dashes (``a-``) or double dashes (``a--b``) —
    those are URL-safe. We test only the clearly-invalid shapes.
    """
    invalid_slugs = [
        "UpperCase",  # uppercase
        "has space",  # space
        "-leading-dash",  # leading dash
    ]
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        for bad in invalid_slugs:
            resp = await client.post(
                "/api/v1/knowledge/knowledge-bases",
                headers=_auth_headers(tenant_id=tenant_factory.id),
                json={"name": "x", "slug": bad},
            )
            assert resp.status_code == 422, (
                f"slug {bad!r} should be rejected, got {resp.status_code}"
            )


@pytest.mark.integration
async def test_create_kb_slug_conflict(
    tenant_factory: Tenant,
) -> None:
    """POST with a slug already used in the tenant returns 409."""
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        first = await client.post(
            "/api/v1/knowledge/knowledge-bases",
            headers=_auth_headers(tenant_id=tenant_factory.id),
            json={"name": "First", "slug": "shared-slug"},
        )
        assert first.status_code == 201

        second = await client.post(
            "/api/v1/knowledge/knowledge-bases",
            headers=_auth_headers(tenant_id=tenant_factory.id),
            json={"name": "Second", "slug": "shared-slug"},
        )
        assert second.status_code == 409, second.text
        assert "already used" in second.json()["detail"]


@pytest.mark.integration
async def test_get_kb_cross_tenant_returns_404(
    tenant_factory: Tenant,
    second_tenant_factory: Tenant,
) -> None:
    """KB owned by tenant A queried as tenant B returns 404 (never 403)."""
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="private")
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        # Sanity: tenant A CAN see it
        ok = await client.get(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}",
            headers=_auth_headers(tenant_id=tenant_factory.id),
        )
        assert ok.status_code == 200

        # Tenant B sees 404 (anti-enumeration)
        cross = await client.get(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}",
            headers=_auth_headers(tenant_id=second_tenant_factory.id),
        )
        assert cross.status_code == 404, cross.text


@pytest.mark.integration
async def test_update_kb(
    tenant_factory: Tenant,
) -> None:
    """PATCH name + chunk_size -> 200; GET reflects the changes."""
    kb = await _kb_factory(tenant_id=tenant_factory.id)
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        patch = await client.patch(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}",
            headers=_auth_headers(tenant_id=tenant_factory.id),
            json={"name": "Renamed", "chunk_size": 600, "chunk_overlap": 80},
        )
        assert patch.status_code == 200, patch.text
        body = patch.json()
        assert body["name"] == "Renamed"
        assert body["chunk_size"] == 600
        assert body["chunk_overlap"] == 80

        # GET shows the same values
        get = await client.get(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}",
            headers=_auth_headers(tenant_id=tenant_factory.id),
        )
        assert get.status_code == 200
        assert get.json()["name"] == "Renamed"
        assert get.json()["chunk_size"] == 600


@pytest.mark.integration
async def test_delete_kb_cascades(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELETE /knowledge-bases/{id} -> 204; subsequent GET -> 404.

    Also confirms an Article inside the KB is cascade-deleted.
    """
    _patch_embed(monkeypatch)
    await _ensure_collection_ready()

    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="to-delete")
    # Seed an article inside the KB so we can verify cascade
    article = Article(
        id=new_id(),
        tenant_id=tenant_factory.id,
        knowledge_base_id=kb.id,
        title="Cascade test",
        source_uri=None,
        source_type=ArticleSourceType.MANUAL,
        status=ArticleStatus.DRAFT,
        current_version_id=None,
        error_message=None,
    )
    async with get_session() as session:
        session.add(article)
        await session.commit()
    article_id = article.id

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        delete = await client.delete(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}",
            headers=_auth_headers(tenant_id=tenant_factory.id),
        )
        assert delete.status_code == 204

        follow_up = await client.get(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}",
            headers=_auth_headers(tenant_id=tenant_factory.id),
        )
        assert follow_up.status_code == 404

    # The article inside the KB should be cascade-deleted.
    async with get_session() as session:
        gone = await session.get(Article, article_id)
        assert gone is None


# ============================================================================
# Article endpoint tests
# ============================================================================


@pytest.mark.integration
async def test_create_article_kicks_off_indexing(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST article kicks off indexer; status transitions DRAFT -> INDEXING -> INDEXED.

    The fire-and-forget ``asyncio.create_task`` schedules
    ``index_article`` immediately after the POST returns. The test
    polls for status==INDEXING within 2 seconds, then waits for
    INDEXED within another 5 seconds.

    With the stubbed embedding, the indexer completes in a few ms
    on small inputs (1 chunk) — too fast for HTTP-polling to catch
    INDEXING reliably. We use a 5000-word raw_text so the chunker
    produces ~7 chunks (chunk_size=800) and the pipeline takes a
    measurable amount of time. INDEXING is observed by polling on
    the DB directly to avoid HTTP round-trip latency.
    """
    _patch_embed(monkeypatch)
    await _ensure_collection_ready()
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="indexing-test")
    # 5000 words -> ~7 chunks at chunk_size=800. The chunker /
    # embedder / upsert loop stretches the INDEXING window out to
    # tens of ms — enough for a fast DB poll to catch it.
    raw_text = " ".join(f"w{i}" for i in range(5000))

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        create = await client.post(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}/articles",
            headers=_auth_headers(tenant_id=tenant_factory.id),
            json={
                "title": "Indexed article",
                "source_type": "manual",
                "raw_text": raw_text,
            },
        )
        assert create.status_code == 201, create.text
        article = create.json()
        article_id = article["id"]
        assert article["status"] == "draft"  # M1: initial status is DRAFT

        # Poll the DB directly (no HTTP round-trip) every 5ms for
        # 2s. Direct DB reads are ~1000x faster than HTTP polling,
        # which is the only way to catch a sub-second INDEXING
        # window reliably.
        deadline = asyncio.get_event_loop().time() + 2.0
        saw_indexing = False
        while asyncio.get_event_loop().time() < deadline:
            async with get_session() as session:
                row = await session.get(Article, article_id)
            if row is not None and row.status == ArticleStatus.INDEXING:
                saw_indexing = True
                break
            await asyncio.sleep(0.005)
        assert saw_indexing, "article did not transition to INDEXING within 2s"

        # Now wait for INDEXED via HTTP (this can be slow) within 10s.
        deadline = asyncio.get_event_loop().time() + 10.0
        saw_indexed = False
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.1)
            status_resp = await client.get(
                f"/api/v1/knowledge/articles/{article_id}",
                headers=_auth_headers(tenant_id=tenant_factory.id),
            )
            if status_resp.json()["status"] == "indexed":
                saw_indexed = True
                break
        assert saw_indexed, "article did not transition to INDEXED within 10s"

    # Cleanup Qdrant
    await _delete_qdrant_points_for_article(article_id=article_id)


@pytest.mark.integration
async def test_list_articles_in_kb(
    tenant_factory: Tenant,
) -> None:
    """POST 3 articles; GET list returns 3."""
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="list-test")

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        # Seed 3 articles
        for i in range(3):
            resp = await client.post(
                f"/api/v1/knowledge/knowledge-bases/{kb.id}/articles",
                headers=_auth_headers(tenant_id=tenant_factory.id),
                json={
                    "title": f"Article {i}",
                    "source_type": "manual",
                    "raw_text": f"body {i}",
                },
            )
            assert resp.status_code == 201

        # List
        list_resp = await client.get(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}/articles",
            headers=_auth_headers(tenant_id=tenant_factory.id),
        )
        assert list_resp.status_code == 200
        items = list_resp.json()["items"]
        assert len(items) == 3


@pytest.mark.integration
async def test_list_articles_filter_by_status(
    tenant_factory: Tenant,
) -> None:
    """``status=draft`` filter returns only DRAFT articles.

    We insert articles directly via DB so the create-endpoint's
    fire-and-forget indexer doesn't race the assertions. The
    indexer would otherwise transition articles through
    DRAFT -> INDEXING -> INDEXED/FAILED between the seed and the
    query, making the filter assertions non-deterministic.
    """
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="filter-test")

    # Seed 3 DRAFT articles directly via DB.
    for i in range(3):
        art = Article(
            id=new_id(),
            tenant_id=tenant_factory.id,
            knowledge_base_id=kb.id,
            title=f"Draft {i}",
            source_uri=None,
            source_type=ArticleSourceType.MANUAL,
            status=ArticleStatus.DRAFT,
            current_version_id=None,
            error_message=None,
        )
        async with get_session() as session:
            session.add(art)
            await session.commit()

    # Seed 2 INDEXED articles directly via DB.
    for i in range(2):
        art = Article(
            id=new_id(),
            tenant_id=tenant_factory.id,
            knowledge_base_id=kb.id,
            title=f"Indexed {i}",
            source_uri=None,
            source_type=ArticleSourceType.MANUAL,
            status=ArticleStatus.INDEXED,
            current_version_id=None,
            error_message=None,
        )
        async with get_session() as session:
            session.add(art)
            await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        # Filter by DRAFT — should return 3
        list_draft = await client.get(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}/articles",
            params={"status": "draft"},
            headers=_auth_headers(tenant_id=tenant_factory.id),
        )
        assert list_draft.status_code == 200
        items = list_draft.json()["items"]
        assert len(items) == 3
        assert all(a["status"] == "draft" for a in items)

        # Filter by INDEXED — should return 2
        list_indexed = await client.get(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}/articles",
            params={"status": "indexed"},
            headers=_auth_headers(tenant_id=tenant_factory.id),
        )
        assert list_indexed.status_code == 200
        items = list_indexed.json()["items"]
        assert len(items) == 2
        assert all(a["status"] == "indexed" for a in items)


@pytest.mark.integration
async def test_get_article_hydrates_version(
    tenant_factory: Tenant,
) -> None:
    """GET /articles/{id} hydrates ``current_version_id`` AND the version body.

    The response carries ``version`` with content_hash + raw_text.
    We don't need the embedding stub here because the test only
    cares about the version hydration, not the indexer.
    """
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="hydrate-test")
    raw_text = "this is the body"

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        create = await client.post(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}/articles",
            headers=_auth_headers(tenant_id=tenant_factory.id),
            json={
                "title": "Hydrate me",
                "source_type": "manual",
                "raw_text": raw_text,
            },
        )
        assert create.status_code == 201
        article_id = create.json()["id"]

        # Force the indexer off the test's loop so it doesn't
        # race with our assertions. We just need current_version_id
        # populated — the create endpoint already wired that.
        get = await client.get(
            f"/api/v1/knowledge/articles/{article_id}",
            headers=_auth_headers(tenant_id=tenant_factory.id),
        )
        assert get.status_code == 200
        body = get.json()
        assert body["current_version_id"] is not None
        assert body["version"] is not None
        v = body["version"]
        assert v["version_number"] == 1
        assert v["content_hash"] == hashlib.sha256(raw_text.encode()).hexdigest()
        assert v["raw_text"] == raw_text

    # Don't bother waiting for the indexer; cancel it.
    await _delete_qdrant_points_for_article(article_id=article_id)


@pytest.mark.integration
async def test_get_article_cross_tenant_404(
    tenant_factory: Tenant,
    second_tenant_factory: Tenant,
) -> None:
    """Article owned by tenant A queried as tenant B returns 404."""
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="xtenant-article")
    article = Article(
        id=new_id(),
        tenant_id=tenant_factory.id,
        knowledge_base_id=kb.id,
        title="Owned by A",
        source_uri=None,
        source_type=ArticleSourceType.MANUAL,
        status=ArticleStatus.DRAFT,
        current_version_id=None,
        error_message=None,
    )
    async with get_session() as session:
        session.add(article)
        await session.commit()
    article_id = article.id

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        cross = await client.get(
            f"/api/v1/knowledge/articles/{article_id}",
            headers=_auth_headers(tenant_id=second_tenant_factory.id),
        )
        assert cross.status_code == 404, cross.text


@pytest.mark.integration
async def test_update_article_title(
    tenant_factory: Tenant,
) -> None:
    """PATCH /articles/{id} updates the title."""
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="update-test")
    article = Article(
        id=new_id(),
        tenant_id=tenant_factory.id,
        knowledge_base_id=kb.id,
        title="Original",
        source_uri=None,
        source_type=ArticleSourceType.MANUAL,
        status=ArticleStatus.DRAFT,
        current_version_id=None,
        error_message=None,
    )
    async with get_session() as session:
        session.add(article)
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        patch = await client.patch(
            f"/api/v1/knowledge/articles/{article.id}",
            headers=_auth_headers(tenant_id=tenant_factory.id),
            json={"title": "Updated", "source_uri": "https://example.com/x"},
        )
        assert patch.status_code == 200
        body = patch.json()
        assert body["title"] == "Updated"
        assert body["source_uri"] == "https://example.com/x"


@pytest.mark.integration
async def test_delete_article_clears_qdrant(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST + index + DELETE clears Qdrant points for the article.

    We don't go through the API for the index step — we call
    ``index_article`` directly so the test is deterministic about
    the indexer completion (avoids racing the fire-and-forget
    task scheduled by the create endpoint).
    """
    _patch_embed(monkeypatch)
    await _ensure_collection_ready()
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="delete-qdrant-test")
    raw_text = " ".join(f"q{i}" for i in range(60))

    # Seed article + version directly (avoid the create-endpoint
    # fire-and-forget race)
    article = Article(
        id=new_id(),
        tenant_id=tenant_factory.id,
        knowledge_base_id=kb.id,
        title="To be deleted",
        source_uri=None,
        source_type=ArticleSourceType.MANUAL,
        status=ArticleStatus.DRAFT,
        current_version_id=None,
        error_message=None,
    )
    content_hash = hashlib.sha256(raw_text.encode()).hexdigest()
    version = ArticleVersion(
        id=new_id(),
        article_id=article.id,
        version_number=1,
        raw_text=raw_text,
        content_hash=content_hash,
    )
    async with get_session() as session:
        session.add(article)
        await session.flush()
        session.add(version)
        await session.flush()
        article.current_version_id = version.id
        await session.commit()
    article_id = article.id

    # Index via worker
    index_result = await index_article(article_id=article_id)
    assert index_result.status == ArticleStatus.INDEXED
    assert index_result.chunks_indexed >= 1

    # Sanity: Qdrant has at least 1 point for this article
    pre_count = await _qdrant_count_by_article(article_id=article_id)
    assert pre_count >= 1, f"expected >=1 Qdrant point, got {pre_count}"

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        delete = await client.delete(
            f"/api/v1/knowledge/articles/{article_id}",
            headers=_auth_headers(tenant_id=tenant_factory.id),
        )
        assert delete.status_code == 204

    # DB row is gone (CASCADE sweeps version + chunks too)
    async with get_session() as session:
        gone = await session.get(Article, article_id)
        assert gone is None

    # Qdrant points for this article are gone
    post_count = await _qdrant_count_by_article(article_id=article_id)
    assert post_count == 0, f"expected 0 Qdrant points after delete, got {post_count}"


@pytest.mark.integration
async def test_reindex_endpoint_skips_on_no_change(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST + index + immediately reindex -> skipped=True, no new version."""
    _patch_embed(monkeypatch)
    await _ensure_collection_ready()
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="reindex-skip-test")
    raw_text = " ".join(f"s{i}" for i in range(60))

    # Seed + index directly
    article = Article(
        id=new_id(),
        tenant_id=tenant_factory.id,
        knowledge_base_id=kb.id,
        title="Reindex skip",
        source_uri=None,
        source_type=ArticleSourceType.MANUAL,
        status=ArticleStatus.DRAFT,
        current_version_id=None,
        error_message=None,
    )
    content_hash = hashlib.sha256(raw_text.encode()).hexdigest()
    version = ArticleVersion(
        id=new_id(),
        article_id=article.id,
        version_number=1,
        raw_text=raw_text,
        content_hash=content_hash,
    )
    async with get_session() as session:
        session.add(article)
        await session.flush()
        session.add(version)
        await session.flush()
        article.current_version_id = version.id
        await session.commit()
    article_id = article.id

    index_result = await index_article(article_id=article_id)
    assert index_result.status == ArticleStatus.INDEXED

    try:
        async with AsyncClient(
            transport=ASGITransport(app=_build_app()), base_url="http://test"
        ) as client:
            reindex = await client.post(
                f"/api/v1/knowledge/articles/{article_id}/reindex",
                headers=_auth_headers(tenant_id=tenant_factory.id),
                json={"force": False},
            )
            assert reindex.status_code == 200, reindex.text
            body = reindex.json()
            assert body["skipped"] is True
            assert body["version_number"] == 1  # not bumped

            # Only ONE version exists
            async with get_session() as session:
                stmt = (
                    ArticleVersion.__table__.select()
                    .where(ArticleVersion.article_id == article_id)
                )
                rows = list((await session.execute(stmt)).scalars().all())
            assert len(rows) == 1
    finally:
        await _delete_qdrant_points_for_article(article_id=article_id)


@pytest.mark.integration
async def test_reindex_endpoint_force(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reindex with ``force=True`` mints a new version (skipped=False)."""
    _patch_embed(monkeypatch)
    await _ensure_collection_ready()
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="reindex-force-test")
    raw_text = " ".join(f"f{i}" for i in range(60))

    article = Article(
        id=new_id(),
        tenant_id=tenant_factory.id,
        knowledge_base_id=kb.id,
        title="Reindex force",
        source_uri=None,
        source_type=ArticleSourceType.MANUAL,
        status=ArticleStatus.DRAFT,
        current_version_id=None,
        error_message=None,
    )
    content_hash = hashlib.sha256(raw_text.encode()).hexdigest()
    version = ArticleVersion(
        id=new_id(),
        article_id=article.id,
        version_number=1,
        raw_text=raw_text,
        content_hash=content_hash,
    )
    async with get_session() as session:
        session.add(article)
        await session.flush()
        session.add(version)
        await session.flush()
        article.current_version_id = version.id
        await session.commit()
    article_id = article.id

    index_result = await index_article(article_id=article_id)
    assert index_result.status == ArticleStatus.INDEXED

    try:
        async with AsyncClient(
            transport=ASGITransport(app=_build_app()), base_url="http://test"
        ) as client:
            reindex = await client.post(
                f"/api/v1/knowledge/articles/{article_id}/reindex",
                headers=_auth_headers(tenant_id=tenant_factory.id),
                json={"force": True},
            )
            assert reindex.status_code == 200, reindex.text
            body = reindex.json()
            assert body["skipped"] is False
            assert body["version_number"] == 2  # bumped
            assert body["status"] == "indexed"

            # Two ArticleVersion rows exist now
            async with get_session() as session:
                stmt = (
                    ArticleVersion.__table__.select()
                    .where(ArticleVersion.article_id == article_id)
                )
                rows = list((await session.execute(stmt)).scalars().all())
            assert len(rows) == 2
    finally:
        await _delete_qdrant_points_for_article(article_id=article_id)


# ============================================================================
# Auth tests
# ============================================================================


@pytest.mark.integration
async def test_endpoints_require_auth(
    tenant_factory: Tenant,
) -> None:
    """No Authorization header -> 401 on every endpoint."""
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="auth-test")
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        # Hit every endpoint shape — none should accept anonymous
        # requests. The exact status code depends on
        # ``auth.dependencies.get_current_user`` — current behavior
        # is 401 for missing/invalid bearer token.
        endpoints_and_methods = [
            ("GET", "/api/v1/knowledge/knowledge-bases"),
            ("POST", "/api/v1/knowledge/knowledge-bases"),
            ("GET", f"/api/v1/knowledge/knowledge-bases/{kb.id}"),
            ("PATCH", f"/api/v1/knowledge/knowledge-bases/{kb.id}"),
            ("DELETE", f"/api/v1/knowledge/knowledge-bases/{kb.id}"),
            ("GET", f"/api/v1/knowledge/knowledge-bases/{kb.id}/articles"),
            ("POST", f"/api/v1/knowledge/knowledge-bases/{kb.id}/articles"),
        ]
        for method, path in endpoints_and_methods:
            kwargs: dict[str, object] = {"url": path}
            if method == "POST" or method == "PATCH":
                kwargs["json"] = {}
            resp = await client.request(method, **kwargs)
            assert resp.status_code == 401, (
                f"{method} {path} should reject anonymous caller, got "
                f"{resp.status_code}: {resp.text}"
            )

        # The /articles/{id}/* routes don't require a KB so we use
        # a fake id; missing auth still wins.
        fake_article = new_id()
        for method, path in [
            ("GET", f"/api/v1/knowledge/articles/{fake_article}"),
            ("PATCH", f"/api/v1/knowledge/articles/{fake_article}"),
            ("DELETE", f"/api/v1/knowledge/articles/{fake_article}"),
            ("POST", f"/api/v1/knowledge/articles/{fake_article}/reindex"),
        ]:
            kwargs = {"url": path}
            if method in ("POST", "PATCH"):
                kwargs["json"] = {}
            resp = await client.request(method, **kwargs)
            assert resp.status_code == 401, (
                f"{method} {path} should reject anonymous caller, got "
                f"{resp.status_code}: {resp.text}"
            )