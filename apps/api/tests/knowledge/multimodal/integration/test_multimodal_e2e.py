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
