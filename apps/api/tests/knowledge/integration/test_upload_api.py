"""Live-DB integration tests for the file-upload endpoints (Task 6.10).

Exercises ``POST /knowledge-bases/{kb_id}/articles/upload`` and
``POST /articles/{article_id}/upload`` against a real Postgres +
Qdrant. Mirrors the patterns in ``test_api_crud.py``:

* ``autouse`` fixture resets the engine / sessionmaker / Qdrant
  singletons between tests.
* Tenants are created per-test; ``finally:`` cascade-deletes.
* ``auth.jwt.create_access_token`` mints real JWTs.
* The indexer is stubbed via ``knowledge.worker.embed_texts`` so
  no OpenAI call lands.

PII discipline
--------------

All uploaded payloads are short synthetic strings (``b"hello"``,
``b"# markdown\\n..."``, etc.). We never embed real documents in
tests.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
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
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository

# ============================================================================
# Fixtures / helpers
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_db_singletons() -> None:
    """Reset async engine / sessionmaker / Qdrant between tests.

    Each pytest-asyncio test runs in its own event loop. Without
    this the engine / Qdrant client from a previous test would
    try to reconnect on a closed loop.
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
        name="Upload Test Tenant", plan=TenantPlan.FREE
    )
    try:
        yield tenant
    finally:
        await _delete_tenant(tenant.id)


@pytest.fixture
async def second_tenant_factory() -> AsyncIterator[Tenant]:
    """Yield a second Tenant for cross-tenant 404 tests."""
    tenant = await TenantRepository().create(
        name="Upload Test Tenant B", plan=TenantPlan.FREE
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


async def _kb_factory(*, tenant_id: str, slug: str = "upload-kb") -> KnowledgeBase:
    """Insert a KnowledgeBase row directly via the model."""
    kb = KnowledgeBase(
        id=new_id(),
        tenant_id=tenant_id,
        name="Upload KB",
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
    """Replace ``knowledge.worker.embed_texts`` with a deterministic stub."""

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
    """Best-effort cleanup of any Qdrant points for an article."""
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


async def _wait_for_indexed(
    *, client: AsyncClient, tenant_id: str, article_id: str, timeout_s: float = 10.0
) -> dict:
    """Poll GET /articles/{id} until status=='indexed' or timeout.

    Returns the final article JSON body. Raises ``AssertionError``
    on timeout.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    body: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
        resp = await client.get(
            f"/api/v1/knowledge/articles/{article_id}",
            headers=_auth_headers(tenant_id=tenant_id),
        )
        if resp.status_code != 200:
            continue
        body = resp.json()
        if body.get("status") == "indexed":
            return body
        if body.get("status") == "failed":
            pytest.fail(f"indexer marked article FAILED: {body.get('error_message')}")
    pytest.fail(f"article did not transition to INDEXED within {timeout_s}s; last={body}")


def _file_upload(
    *,
    name: str,
    payload: bytes,
    mime_type: str = "text/plain",
) -> dict:
    """Build the ``files=`` kwarg for httpx multipart uploads.

    httpx expects ``{"<field>": (, <buffer>, <mime>)}`` —
    we accept a buffer so callers can pass ``io.BytesIO`` for
    large / synthetic payloads.
    """
    return {"file": (name, io.BytesIO(payload), mime_type)}


# ============================================================================
# Tests
# ============================================================================


@pytest.mark.integration
async def test_upload_text_file_creates_article(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upload a small .txt file -> 201, status transitions to INDEXED, Qdrant has 1+ points."""
    _patch_embed(monkeypatch)
    await _ensure_collection_ready()
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="upload-text")
    payload = "hello world this is a test document with some words\n".encode("utf-8")

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}/articles/upload",
            headers=_auth_headers(tenant_id=tenant_factory.id),
            files=_file_upload(name="greeting.txt", payload=payload),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        article_id = body["id"]
        # Title defaulted from filename
        assert body["title"] == "greeting"
        assert body["source_type"] == "upload"
        assert body["status"] == "draft"  # M1: initial status is DRAFT

        # Wait for indexer (fire-and-forget) to settle
        final = await _wait_for_indexed(
            client=client, tenant_id=tenant_factory.id, article_id=article_id
        )
        assert final["status"] == "indexed"

    # Qdrant has at least 1 point for this article
    count = await _qdrant_count_by_article(article_id=article_id)
    assert count >= 1, f"expected >=1 Qdrant point, got {count}"

    # DB has the v1 ArticleVersion with the parsed text
    from sqlalchemy import select

    async with get_session() as session:
        stmt = select(ArticleVersion).where(ArticleVersion.article_id == article_id)
        rows = list((await session.execute(stmt)).scalars().all())
    assert len(rows) == 1
    assert rows[0].version_number == 1
    assert rows[0].content_hash == hashlib.sha256(payload.decode("utf-8").encode("utf-8")).hexdigest()

    await _delete_qdrant_points_for_article(article_id=article_id)


@pytest.mark.integration
async def test_upload_markdown_file_with_code_block(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upload .md with a fenced code block -> status INDEXED, chunks persisted.

    M1 scope note (Task 6.10 explicitly forbids modifying
    ``index_article``): the worker re-parses ``raw_text`` as plain
    text, so the markdown source format that the upload pipeline
    detected is NOT re-detected downstream. This test verifies
    the upload path (parser + persistence + indexer kickoff) end
    to end. Re-extracting code blocks during indexing requires a
    small worker change (Task 6.7.x) — tracked separately.
    """
    _patch_embed(monkeypatch)
    await _ensure_collection_ready()
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="upload-md")
    md_payload = (
        "# Heading\n\n"
        "Some prose that talks about a thing.\n\n"
        "```python\n"
        "def greet(name):\n"
        "    return f'hi {name}'\n"
        "```\n\n"
        "And some trailing prose after the code.\n"
    ).encode("utf-8")

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}/articles/upload",
            headers=_auth_headers(tenant_id=tenant_factory.id),
            files=_file_upload(
                name="notes.md", payload=md_payload, mime_type="text/markdown"
            ),
        )
        assert resp.status_code == 201, resp.text
        article_id = resp.json()["id"]
        # Title defaulted from filename (without .md)
        assert resp.json()["title"] == "notes"

        final = await _wait_for_indexed(
            client=client, tenant_id=tenant_factory.id, article_id=article_id
        )
        assert final["status"] == "indexed"

    # At least one Chunk row was persisted. Block-level (code)
    # extraction isn't re-run by the M1 worker (see scope note
    # above), so we don't assert block_type=='code'.
    from sqlalchemy import select

    from knowledge.models import Chunk

    async with get_session() as session:
        stmt = (
            select(Chunk)
            .where(Chunk.article_id == article_id)
            .order_by(Chunk.chunk_index.asc())
        )
        rows = list((await session.execute(stmt)).scalars().all())
    assert len(rows) >= 1, f"expected at least one chunk, got {rows!r}"

    await _delete_qdrant_points_for_article(article_id=article_id)


@pytest.mark.integration
async def test_upload_oversize_returns_413(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upload > MAX_PARSE_BYTES (50 MiB + 1) returns 413 at the API cap.

    The API enforces the cap BEFORE the parser sees the bytes, so
    the parser's own ``OversizeDocumentError`` (which would map to
    422) is a defense-in-depth path that's not exercised here.
    """
    _patch_embed(monkeypatch)
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="oversize")

    # 50 MiB + 1 byte to exceed the cap by one byte. We build the
    # payload as a buffer so httpx doesn't have to materialise
    # 50+ MiB through Python bytes intermediates twice.
    oversize_buffer = io.BytesIO(b"x" * (50 * 1024 * 1024 + 1))

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}/articles/upload",
            headers=_auth_headers(tenant_id=tenant_factory.id),
            files={
                "file": ("big.txt", oversize_buffer, "text/plain"),
            },
        )
        # Spec says 413 OR 422 is acceptable; we enforce the cap
        # at the API edge so we return 413.
        assert resp.status_code == 413, resp.text
        assert "MiB" in resp.json()["detail"]

        # No article was created.
        from sqlalchemy import func, select

        async with get_session() as session:
            count_stmt = (
                select(func.count())
                .select_from(Article)
                .where(Article.knowledge_base_id == kb.id)
            )
            total = (await session.execute(count_stmt)).scalar_one()
        assert total == 0, f"expected 0 articles after oversize rejection, got {total}"


@pytest.mark.integration
async def test_upload_unsupported_mime_returns_error(
    tenant_factory: Tenant,
) -> None:
    """Upload a .bin / .exe with binary content + unknown mime -> 422.

    The parser's ``_looks_like_utf8_text`` sniff rejects content
    that contains NUL bytes, so a binary blob raises
    ``UnsupportedDocumentType`` and the API maps that to 422.

    This documents the actual behavior: unsupported payloads are
    NOT silently accepted as empty documents.
    """
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="unsupported")
    # Binary content with NUL bytes — fails the text sniff.
    binary_payload = b"\x00\x01\x02\x03\x04not-a-text-file\x00\x00"

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}/articles/upload",
            headers=_auth_headers(tenant_id=tenant_factory.id),
            files=_file_upload(
                name="prog.exe", payload=binary_payload, mime_type="application/octet-stream"
            ),
        )
        # 422: parser raised UnsupportedDocumentType.
        assert resp.status_code == 422, resp.text


@pytest.mark.integration
async def test_reupload_changes_version_when_content_differs(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reupload with different content -> v2 created, current_version_id updated."""
    _patch_embed(monkeypatch)
    await _ensure_collection_ready()
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="reupload-diff")

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        # v1
        first = await client.post(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}/articles/upload",
            headers=_auth_headers(tenant_id=tenant_factory.id),
            files=_file_upload(
                name="v1.txt", payload=b"version one body\n"
            ),
        )
        assert first.status_code == 201
        article_id = first.json()["id"]

        # Wait for v1 to be INDEXED so the reupload pipeline is in
        # a stable state.
        await _wait_for_indexed(
            client=client, tenant_id=tenant_factory.id, article_id=article_id
        )

        # v2 — different content
        reupload = await client.post(
            f"/api/v1/knowledge/articles/{article_id}/upload",
            headers=_auth_headers(tenant_id=tenant_factory.id),
            files=_file_upload(
                name="v2.txt", payload=b"version two body completely different\n"
            ),
        )
        assert reupload.status_code == 200, reupload.text
        body = reupload.json()
        assert body["skipped"] is False
        assert body["version_number"] == 2
        assert body["status"] == "indexed"
        assert body["chunks_indexed"] >= 1

        # Two ArticleVersion rows exist
        from sqlalchemy import select

        async with get_session() as session:
            stmt = (
                select(ArticleVersion)
                .where(ArticleVersion.article_id == article_id)
                .order_by(ArticleVersion.version_number.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        assert len(rows) == 2
        assert [r.version_number for r in rows] == [1, 2]

        # current_version_id points at v2
        async with get_session() as session:
            article = await session.get(Article, article_id)
        assert article is not None
        assert article.current_version_id == rows[1].id
        assert article.status == ArticleStatus.INDEXED

    await _delete_qdrant_points_for_article(article_id=article_id)


@pytest.mark.integration
async def test_reupload_skips_when_content_unchanged(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reupload with IDENTICAL content -> skipped=True, no new version."""
    _patch_embed(monkeypatch)
    await _ensure_collection_ready()
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="reupload-same")
    payload = b"identical body bytes\n"

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        first = await client.post(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}/articles/upload",
            headers=_auth_headers(tenant_id=tenant_factory.id),
            files=_file_upload(name="same.txt", payload=payload),
        )
        assert first.status_code == 201
        article_id = first.json()["id"]
        await _wait_for_indexed(
            client=client, tenant_id=tenant_factory.id, article_id=article_id
        )

        # Reupload the SAME content
        reupload = await client.post(
            f"/api/v1/knowledge/articles/{article_id}/upload",
            headers=_auth_headers(tenant_id=tenant_factory.id),
            files=_file_upload(name="same.txt", payload=payload),
        )
        assert reupload.status_code == 200, reupload.text
        body = reupload.json()
        assert body["skipped"] is True
        assert body["version_number"] == 1  # not bumped
        assert body["chunks_indexed"] == 0

        # Only one ArticleVersion row
        from sqlalchemy import select

        async with get_session() as session:
            stmt = select(ArticleVersion).where(
                ArticleVersion.article_id == article_id
            )
            rows = list((await session.execute(stmt)).scalars().all())
        assert len(rows) == 1

    await _delete_qdrant_points_for_article(article_id=article_id)


@pytest.mark.integration
async def test_upload_cross_tenant_returns_404(
    tenant_factory: Tenant,
    second_tenant_factory: Tenant,
) -> None:
    """Tenant B uploading to tenant A's KB -> 404 (anti-enumeration)."""
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="xtenant-upload")

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/knowledge/knowledge-bases/{kb.id}/articles/upload",
            headers=_auth_headers(tenant_id=second_tenant_factory.id),
            files=_file_upload(name="leak.txt", payload=b"hi"),
        )
        assert resp.status_code == 404, resp.text


@pytest.mark.integration
async def test_upload_requires_auth(
    tenant_factory: Tenant,
) -> None:
    """No Authorization header on the upload endpoints -> 401."""
    kb = await _kb_factory(tenant_id=tenant_factory.id, slug="upload-auth")

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        for path in [
            f"/api/v1/knowledge/knowledge-bases/{kb.id}/articles/upload",
            f"/api/v1/knowledge/articles/{new_id()}/upload",
        ]:
            resp = await client.post(
                path,
                files=_file_upload(name="x.txt", payload=b"x"),
            )
            assert resp.status_code == 401, (
                f"{path} should reject anonymous caller, got "
                f"{resp.status_code}: {resp.text}"
            )
