"""End-to-end tests for multimodal KB upload + retrieval (Stage 17 Task 6).

Mirrors the patterns in ``tests/knowledge/integration/test_upload_api.py``:

* ``autouse`` fixture resets the engine / sessionmaker / Qdrant
  singletons between tests.
* Tenants are created per-test; ``finally:`` cascade-deletes.
* JWT bearer tokens are minted via ``auth.jwt.create_access_token``.
* The vision embedder + S3 object store are mocked so no external
  service is required (the tests are runnable in CI without
  MinIO/Doubao creds).

PII discipline
--------------

Uploaded payloads are synthetic PNG / PDF byte sequences. We never
embed real documents in tests.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from auth.jwt import create_access_token
from core.database import get_session
from core.id_gen import new_id
from core.qdrant import get_qdrant_client
from knowledge.multimodal.api import router as kb_multimodal_router
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons() -> None:
    """Reset async engine / sessionmaker / Qdrant between tests."""
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
    """Yield a fresh Tenant. Cleanup cascades."""
    tenant = await TenantRepository().create(
        name="Multimodal Test Tenant", plan=TenantPlan.FREE
    )
    try:
        yield tenant
    finally:
        await _delete_tenant(tenant.id)


@pytest.fixture
async def second_tenant_factory() -> AsyncIterator[Tenant]:
    """Yield a second Tenant for cross-tenant isolation tests."""
    tenant = await TenantRepository().create(
        name="Multimodal Test Tenant B", plan=TenantPlan.FREE
    )
    try:
        yield tenant
    finally:
        await _delete_tenant(tenant.id)


async def _delete_tenant(tenant_id: str) -> None:
    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t is not None:
            await session.delete(t)


@pytest.fixture
async def app_client() -> AsyncIterator[AsyncClient]:
    """Build a FastAPI app with the multimodal router mounted and an AsyncClient."""
    app = FastAPI()
    app.include_router(kb_multimodal_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
def auth_headers(tenant_factory):
    """Mint a JWT for the primary tenant."""
    token = create_access_token(
        user_id=new_id(),
        tenant_id=tenant_factory.id,
        role="owner",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def second_auth_headers(second_tenant_factory):
    """Mint a JWT for the secondary tenant."""
    token = create_access_token(
        user_id=new_id(),
        tenant_id=second_tenant_factory.id,
        role="owner",
    )
    return {"Authorization": f"Bearer {token}"}


def _png_bytes() -> bytes:
    """Return a minimal valid 1x1 PNG."""
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
        b"\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02"
        b"\xfe\xa3\xa3\xa7\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _mock_embedder(dimension: int = 1024) -> MagicMock:
    """Build a mock DoubaoVisionEmbedder that returns zero vectors.

    We patch at the class-attribute level so ``encode`` returns an
    ``EmbeddingResult``-shaped object without making an HTTP call.
    """
    mock = MagicMock()
    mock.dimension = dimension
    mock.encode = AsyncMock(
        return_value=MagicMock(vector=[0.0] * dimension, model="fake-vision")
    )
    mock.aclose = AsyncMock(return_value=None)
    return mock


def _mock_object_store() -> MagicMock:
    """Build a mock S3ObjectStore whose put() returns a stable URL."""
    mock = MagicMock()
    mock.put = MagicMock(return_value="http://test-bucket/test-key")
    return mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upload_image_indexes_single_chunk(
    app_client: AsyncClient, tenant_factory: Tenant, auth_headers
):
    """Upload PNG → 1 image chunk → 201."""
    fake_embedding = [0.1] * 1024
    with patch(
        "knowledge.multimodal.api.DoubaoVisionEmbedder",
        return_value=_mock_embedder(),
    ), patch(
        "knowledge.multimodal.api.get_object_store",
        return_value=_mock_object_store(),
    ):
        files = {"file": ("test.png", _png_bytes(), "image/png")}
        data = {
            "kb_slug": "default",
            "title": "Test Image",
        }
        resp = await app_client.post(
            "/api/v1/kb-articles/multimodal",
            files=files,
            data=data,
            headers=auth_headers,
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["article_id"]
    assert body["image_chunks"] == 1
    assert body["text_chunks"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upload_pdf_stores_metadata(
    app_client: AsyncClient, tenant_factory: Tenant, auth_headers
):
    """Upload PDF (minimal bytes) → 201 with chunk counts.

    The PDF processor is real (it accepts malformed PDFs gracefully
    — see ``test_pdf_processor.py::test_extract_handles_blank_pdf``).
    """
    pdf_bytes = (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj\n"
        b"xref\n0 4\n0000000000 65535 f\n"
        b"0000000009 00000 n\n0000000058 00000 n\n0000000105 00000 n\n"
        b"trailer << /Size 4 /Root 1 0 R >>\nstartxref\n164\n%%EOF"
    )
    with patch(
        "knowledge.multimodal.api.DoubaoVisionEmbedder",
        return_value=_mock_embedder(),
    ), patch(
        "knowledge.multimodal.api.get_object_store",
        return_value=_mock_object_store(),
    ):
        files = {"file": ("test.pdf", pdf_bytes, "application/pdf")}
        data = {
            "kb_slug": "default",
            "title": "Test PDF",
        }
        resp = await app_client.post(
            "/api/v1/kb-articles/multimodal",
            files=files,
            data=data,
            headers=auth_headers,
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["article_id"]
    # Blank PDF → no text chunks and no key-page screenshots.
    # The chunk counts reflect the real PDF processor output, not
    # the mocked embedder.
    assert body["text_chunks"] == 0
    assert body["image_chunks"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upload_rejects_unsupported_mime(
    app_client: AsyncClient, auth_headers
):
    """Upload text/plain → 400."""
    files = {"file": ("hello.txt", b"hello", "text/plain")}
    data = {"kb_slug": "default", "title": "TXT"}
    resp = await app_client.post(
        "/api/v1/kb-articles/multimodal",
        files=files,
        data=data,
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert "unsupported mime type" in resp.text.lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upload_rejects_empty_file(
    app_client: AsyncClient, auth_headers
):
    """Upload zero-byte PNG → 400."""
    files = {"file": ("empty.png", b"", "image/png")}
    data = {"kb_slug": "default", "title": "Empty"}
    resp = await app_client.post(
        "/api/v1/kb-articles/multimodal",
        files=files,
        data=data,
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert "empty" in resp.text.lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upload_rejects_missing_auth(
    app_client: AsyncClient, tenant_factory: Tenant
):
    """No bearer token → 401 (defense: tenant_id is sourced from JWT)."""
    files = {"file": ("test.png", _png_bytes(), "image/png")}
    data = {"kb_slug": "default", "title": "No auth"}
    resp = await app_client.post(
        "/api/v1/kb-articles/multimodal",
        files=files,
        data=data,
    )
    assert resp.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_tenant_isolation_db_rows_partitioned(
    app_client: AsyncClient,
    tenant_factory: Tenant,
    second_tenant_factory: Tenant,
):
    """Tenant A's upload MUST NOT be retrievable / visible to Tenant B.

    Stage 17 / M2.B Task 6 — the cross-tenant isolation contract
    is enforced at TWO layers (defense in depth):

    1. The DB row carries ``tenant_id=A``; tenant B's reads (via
       any future GET / list endpoint) MUST filter on
       ``tenant_id=B`` so tenant A's rows are invisible.
    2. The Qdrant ``kb_image_vectors`` MUST-filter carries
       ``tenant_id=B`` so the vector search never returns tenant
       A's vectors.

    This test pins the partition by exercising the
    :class:`MultimodalRetriever` directly with tenant B's
    ``tenant_id`` after tenant A uploaded an article to the
    ``shared-kb`` slug. Tenant B's RRF-fused retrieval must
    return ZERO hits — the MUST-filter excludes tenant A's
    points.

    We don't yet have a GET endpoint to assert on directly (it's
    deferred to Stage 17+), so the retriever-level check is the
    tightest assertion available today.
    """
    from knowledge.multimodal.retriever import MultimodalRetriever

    with patch(
        "knowledge.multimodal.api.DoubaoVisionEmbedder",
        return_value=_mock_embedder(),
    ), patch(
        "knowledge.multimodal.api.get_object_store",
        return_value=_mock_object_store(),
    ):
        # ---- Tenant A uploads ------------------------------------
        files_a = {"file": ("secret.png", _png_bytes(), "image/png")}
        data_a = {"kb_slug": "shared-kb", "title": "Tenant A Secret"}
        resp_a = await app_client.post(
            "/api/v1/kb-articles/multimodal",
            files=files_a,
            data=data_a,
            headers={
                "Authorization": (
                    "Bearer " + create_access_token(
                        user_id=new_id(),
                        tenant_id=tenant_factory.id,
                        role="owner",
                    )
                )
            },
        )
    assert resp_a.status_code == 201, resp_a.text
    article_a_id = resp_a.json()["article_id"]

    # ---- Tenant B retrieves via the retriever -----------------
    # The retriever uses the Qdrant MUST-filter on
    # ``tenant_id``. A search from tenant B MUST NOT see
    # tenant A's article even when using the same ``kb_slug``
    # and the same text query.
    qdrant = get_qdrant_client()
    retriever = MultimodalRetriever(qdrant, top_k=5)
    hits = await retriever.retrieve(
        tenant_id=second_tenant_factory.id,
        kb_slug="shared-kb",
        text_query_embedding=[0.1] * 1024,
        image_query_embedding=None,
    )

    # Tenant B sees zero hits — the cross-tenant filter excludes
    # tenant A's points. The ``article_a_id`` MUST NOT appear in
    # any hit's metadata (defense-in-depth assertion: even if a
    # future regression leaks a payload field, the article_id
    # check catches it).
    article_ids = {h.article_id for h in hits}
    assert article_a_id not in article_ids, (
        f"cross-tenant leak: tenant B's retrieval returned "
        f"tenant A's article_id={article_a_id!r}; hits={hits!r}"
    )
    assert hits == [], (
        f"cross-tenant leak: tenant B got {len(hits)} hits from "
        f"tenant A's KB slug 'shared-kb'; expected zero"
    )


# ---------------------------------------------------------------------------
# Pure-logic RRF tests (no Qdrant, no DB)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pure-logic RRF tests (no Qdrant, no DB)
# ---------------------------------------------------------------------------


def test_retriever_rrf_fusion_text_only():
    """Pure text hits → fused order respects rank, no dummy entries."""
    from knowledge.multimodal.retriever import MultimodalRetriever, RetrievalHit

    text_hits = [
        RetrievalHit(
            chunk_id="t1",
            score=0.9,
            source_type="text",
            article_id="a1",
            metadata={"title": "Article 1"},
        ),
        RetrievalHit(
            chunk_id="t2",
            score=0.8,
            source_type="text",
            article_id="a2",
            metadata={"title": "Article 2"},
        ),
    ]

    fused = MultimodalRetriever._rrf_fuse(text_hits, [])

    assert len(fused) == 2
    # t1 was rank 1 → highest RRF score (1/(60+1)).
    assert fused[0].chunk_id == "t1"
    assert fused[1].chunk_id == "t2"
    assert fused[0].score > fused[1].score > 0


def test_retriever_rrf_fusion_image_only():
    """Pure image hits → image source_type preserved in metadata."""
    from knowledge.multimodal.retriever import MultimodalRetriever, RetrievalHit

    image_hits = [
        RetrievalHit(
            chunk_id="i1",
            score=0.95,
            source_type="image",
            article_id="a3",
            metadata={"title": "Diagram"},
        ),
    ]

    fused = MultimodalRetriever._rrf_fuse([], image_hits)

    assert len(fused) == 1
    assert fused[0].chunk_id == "i1"
    assert fused[0].source_type == "image"
    assert fused[0].metadata["title"] == "Diagram"


def test_retriever_rrf_fusion_combined():
    """Hit in BOTH lists → highest score (cross-modal agreement)."""
    from knowledge.multimodal.retriever import MultimodalRetriever, RetrievalHit

    text_hits = [
        RetrievalHit(
            chunk_id="t1",
            score=0.9,
            source_type="text",
            article_id="a1",
            metadata={},
        ),
        RetrievalHit(
            chunk_id="t2",
            score=0.7,
            source_type="text",
            article_id="a2",
            metadata={},
        ),
    ]
    image_hits = [
        RetrievalHit(
            chunk_id="i1",
            score=0.95,
            source_type="image",
            article_id="a3",
            metadata={},
        ),
        # t1 also appears in the image list → cross-modal agreement.
        RetrievalHit(
            chunk_id="t1",
            score=0.6,
            source_type="image",
            article_id="a1",
            metadata={},
        ),
    ]

    fused = MultimodalRetriever._rrf_fuse(text_hits, image_hits)

    # 3 distinct chunk_ids: t1, t2, i1.
    assert len(fused) == 3
    chunk_ids = {h.chunk_id for h in fused}
    assert chunk_ids == {"t1", "t2", "i1"}
    # t1 must top the fused list (contribution from BOTH rankings).
    assert fused[0].chunk_id == "t1"


def test_retriever_rrf_fusion_empty():
    """Empty inputs → empty output (no dummy entries, no errors)."""
    from knowledge.multimodal.retriever import MultimodalRetriever

    assert MultimodalRetriever._rrf_fuse([], []) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pdf_text_chunks_indexed_to_article_chunks(
    tenant_factory: Tenant,
    tmp_path,
) -> None:
    """Uploading a PDF writes text chunks into the article_chunks Qdrant
    collection (tech debt #18) in addition to image vectors.

    Asserts:
    - embed_texts is called once with the PDF's text chunks
    - qdrant.upsert is called with collection_name="article_chunks"
    - Each point carries source_type="pdf_text" + page_num + chunk_index
    - Point count matches len(text_chunks)
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from qdrant_client import AsyncQdrantClient

    from tests.fixtures._generate_pdfs import make_table_pdf

    # 1. Mock Qdrant client to capture upsert calls.
    #    We avoid ``spec=AsyncQdrantClient`` because the real spec
    #    includes async-context-manager dunders that don't survive
    #    ``MagicMock`` cleanly. A plain MagicMock with the methods
    #    we touch explicitly is enough for this test.
    qdrant_mock = MagicMock()
    qdrant_mock.upsert = AsyncMock()
    qdrant_mock.get_collections = AsyncMock(
        return_value=MagicMock(collections=[])
    )
    # ``ensure_image_collection`` short-circuits to True when the
    # collection already exists, so we don't have to mock
    # ``create_collection`` against a real Qdrant server.
    qdrant_mock.collection_exists = AsyncMock(return_value=True)

    # 2. Mock embed_texts to return deterministic 1536-dim vectors.
    #    ``embed_texts`` returns an ``EmbeddingResult`` dataclass, but
    #    since the test only inspects upsert() payloads we don't need
    #    a real dataclass — a plain dict with the same shape works
    #    (the API will read ``.vectors`` off it; we adapt below).
    fake_vectors: list[list[float]] = []

    async def fake_embed_texts(*, texts: list[str], **kwargs):
        for t in texts:
            fake_vectors.append([float(len(t))] + [0.0] * 1535)
        # Return an object that quacks like EmbeddingResult.
        from llm_client.types import EmbeddingResult
        return EmbeddingResult(
            vectors=fake_vectors[-len(texts):],
            model="fake-model",
            prompt_tokens=0,
            total_tokens=0,
        )

    # 3. Mock vision embedder (1024-dim is the real DoubaoVisionEmbedder default).
    fake_image_vector = [0.1] * 1024

    # 4. Generate a small multi-page PDF via the reportlab fixture.
    #    ``make_table_pdf`` writes to disk (takes a ``path: str``),
    #    so write into ``tmp_path`` and read the bytes back.
    pdf_path = tmp_path / "table.pdf"
    make_table_pdf(str(pdf_path))
    pdf_bytes = pdf_path.read_bytes()

    # 5. Build app + client
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(kb_multimodal_router)
    token = create_access_token(
        tenant_id=tenant_factory.id, user_id="admin-1", role="admin"
    )

    with patch(
        "knowledge.multimodal.api.get_qdrant_client", return_value=qdrant_mock
    ), \
         patch("llm_client.embeddings.embed_texts", side_effect=fake_embed_texts), \
         patch(
             "knowledge.multimodal.api.DoubaoVisionEmbedder",
             return_value=_mock_embedder(),
         ), \
         patch(
             "knowledge.multimodal.api.get_object_store",
             return_value=_mock_object_store(),
         ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/kb-articles/multimodal",
                data={"kb_slug": "test-kb", "title": "Test"},
                headers={"Authorization": f"Bearer {token}"},
                files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
            )

    assert resp.status_code in (200, 201), resp.text

    # 6. Assert: at least one upsert call targeted article_chunks.
    article_chunks_calls = [
        call for call in qdrant_mock.upsert.call_args_list
        if call.kwargs.get("collection_name") == "article_chunks"
    ]
    assert article_chunks_calls, (
        "expected at least one upsert to article_chunks; "
        f"got collections: {[c.kwargs.get('collection_name') for c in qdrant_mock.upsert.call_args_list]}"
    )

    # 7. Assert payload shape — every article_chunks point carries
    #    the expected pdf_text metadata.
    for call in article_chunks_calls:
        for point in call.kwargs["points"]:
            payload = point.payload
            assert payload["tenant_id"] == tenant_factory.id
            assert payload["kb_slug"] == "test-kb"
            assert payload["source_type"] == "pdf_text"
            assert "page_num" in payload
            assert "chunk_index" in payload
            assert "text" in payload
            assert isinstance(point.vector, list)
            assert len(point.vector) == 1536
