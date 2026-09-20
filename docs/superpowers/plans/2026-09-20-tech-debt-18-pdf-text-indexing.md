# Tech Debt #18 — PDF Text Chunks Indexed to article_chunks

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Index PDF text chunks (extracted by `PdfProcessor`) into the existing `article_chunks` Qdrant collection so the agent graph retrieves PDF text alongside regular text articles in a single `search_kb` / `search_multimodal_kb` call.

**Architecture:** Inside `upload_multimodal`, after the image-vector upsert block, add a parallel block that (a) calls `embed_texts()` for each text chunk, (b) upserts to `DEFAULT_COLLECTION` ("article_chunks") with `source_type="pdf_text"` + `page_num` + `chunk_index` in the payload. Reuses the existing `MultimodalRetriever` (RRF across `article_chunks` + `kb_image_vectors`) without changes.

**Tech Stack:** FastAPI, qdrant-client, OpenAI-compatible embeddings API (`llm_client.embeddings.embed_texts`), pytest, pytest-httpx.

---

## File Structure

| Path | Role |
|------|------|
| `apps/api/src/knowledge/multimodal/api.py` | `upload_multimodal` — add text-chunk embed + Qdrant upsert block |
| `apps/api/tests/knowledge/multimodal/integration/test_multimodal_e2e.py` | New e2e test asserting PDF text is indexed to `article_chunks` |

No new files. No model migrations (existing `text_chunks_count` / `image_chunks_count` columns are reused).

---

## Task 1: Add the failing e2e test

**Files:**
- Modify: `apps/api/tests/knowledge/multimodal/integration/test_multimodal_e2e.py` (append new test)

- [ ] **Step 1: Inspect the existing test patterns**

Read the rest of `test_multimodal_e2e.py` (lines 80+) to find:
- The existing PDF upload test (uses `_make_table_pdf()` or similar fixture from `_generate_pdfs.py`)
- How `DoubaoVisionEmbedder` is mocked (`patch(...)` with `AsyncMock`)
- How Qdrant upsert is captured (`qdrant.upsert = AsyncMock()` or similar)

Note the test file path: `apps/api/tests/knowledge/multimodal/integration/test_multimodal_e2e.py`.

- [ ] **Step 2: Append the new test**

At the end of `test_multimodal_e2e.py`, add:

```python
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
    from tests.knowledge.multimodal.unit._generate_pdfs import (
        make_table_pdf,
    )  # may need different import; see existing test
    from unittest.mock import AsyncMock, MagicMock, patch
    from qdrant_client import AsyncQdrantClient

    # 1. Mock Qdrant client to capture upsert calls
    qdrant_mock = MagicMock(spec=AsyncQdrantClient)
    qdrant_mock.upsert = AsyncMock()
    qdrant_mock.get_collections = AsyncMock(
        return_value=MagicMock(collections=[])
    )

    # 2. Mock embed_texts to return deterministic 1536-dim vectors (one per chunk)
    fake_vectors: list[list[float]] = []

    async def fake_embed_texts(*, texts: list[str], **kwargs) -> dict:
        for t in texts:
            fake_vectors.append([float(len(t))] + [0.0] * 1535)
        return {
            "vectors": fake_vectors[-len(texts):],
            "model": "fake-model",
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }

    # 3. Mock vision embedder (existing pattern in the file)
    fake_image_vector = [0.1] * 1024

    # 4. Generate a small multi-page PDF
    pdf_bytes = make_table_pdf(pages=2)  # adjust if fixture signature differs

    # 5. Build app + client
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(kb_multimodal_router)
    token = create_access_token(
        tenant_id=tenant_factory.id, user_id="admin-1", role="admin"
    )

    with patch("core.qdrant.get_qdrant_client", return_value=qdrant_mock), \
         patch("llm_client.embeddings.embed_texts", side_effect=fake_embed_texts), \
         patch("knowledge.multimodal.embedder.DoubaoVisionEmbedder.encode",
               new=AsyncMock(return_value=MagicMock(
                   vector=fake_image_vector, model="fake"
               ))):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/kb-articles/multimodal",
                params={"kb_slug": "test-kb", "title": "Test"},
                headers={"Authorization": f"Bearer {token}"},
                files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
            )

    assert resp.status_code in (200, 201), resp.text

    # 6. Assert: at least one upsert call targeted article_chunks
    article_chunks_calls = [
        call for call in qdrant_mock.upsert.call_args_list
        if call.kwargs.get("collection_name") == "article_chunks"
    ]
    assert article_chunks_calls, (
        "expected at least one upsert to article_chunks; "
        f"got collections: {[c.kwargs.get('collection_name') for c in qdrant_mock.upsert.call_args_list]}"
    )

    # 7. Assert payload shape
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
            assert len(point.vector) == 1536  # default embedding dim
```

Adjust the fixture import + `make_table_pdf` signature to match the existing test file's conventions (the explore agent's report flagged that `_generate_pdfs.py` provides `make_table_pdf` and `make_image_pdf`).

- [ ] **Step 3: Run the test — verify it fails**

Run:
```bash
cd apps/api && pytest tests/knowledge/multimodal/integration/test_multimodal_e2e.py::test_pdf_text_chunks_indexed_to_article_chunks -v
```
Expected: FAIL. The current `upload_multimodal` does NOT call `embed_texts` and does NOT upsert to `article_chunks`. The assertion `assert article_chunks_calls` will fail because all upsert calls target `kb_image_vectors`.

- [ ] **Step 4: Commit the failing test**

```bash
git add apps/api/tests/knowledge/multimodal/integration/test_multimodal_e2e.py
git commit -m "test(multimodal): failing test for PDF text chunks → article_chunks"
```

---

## Task 2: Implement the PDF text indexing block

**Files:**
- Modify: `apps/api/src/knowledge/multimodal/api.py` (insert new block between lines 270 and 272)

- [ ] **Step 1: Add the import at the top of the file**

Find the existing import block at the top of `api.py`. Add (anywhere with the other `from knowledge...` or `from llm_client...` imports):

```python
from knowledge.qdrant_client import DEFAULT_COLLECTION
from llm_client.embeddings import EmbeddingError, embed_texts
```

If these are already imported, no change needed.

- [ ] **Step 2: Insert the text-indexing block**

Find the line that begins the image-vectors upsert section (around line 272 — ` # 4. Insert image vectors into the kb_image_vectors Qdrant`). Insert the new block **before** it (so it runs in parallel with image indexing — both before the DB row insert):

```python
    # 3.5. Index PDF text chunks into the article_chunks collection
    #      (tech debt #18). Mirrors knowledge/worker.py:index_article payload
    #      keys (tenant_id, kb_slug, article_id) plus source_type=pdf_text +
    #      page_num + chunk_index. The MultimodalRetriever's existing RRF
    #      merge over DEFAULT_COLLECTION + kb_image_vectors then returns
    #      PDF text in the same result set as regular KB articles.
    if text_chunks:
        try:
            chunk_texts = [text for _page, text in text_chunks]
            embed_result = await embed_texts(
                texts=chunk_texts,
                tenant_id=tenant_id,
            )
            from qdrant_client.models import PointStruct

            text_points = [
                PointStruct(
                    id=new_id(),
                    vector=vec,
                    payload={
                        "tenant_id": tenant_id,
                        "kb_slug": kb_slug,
                        "article_id": article_id,
                        "source_type": "pdf_text",
                        "page_num": page_num,
                        "chunk_index": idx,
                        "text": text,
                    },
                )
                for idx, ((page_num, text), vec)
                in enumerate(zip(text_chunks, embed_result["vectors"]))
            ]
            await qdrant.upsert(
                collection_name=DEFAULT_COLLECTION,
                points=text_points,
                wait=True,
            )
        except EmbeddingError as exc:
            log.warning(
                "kb.multimodal.text_embedding_failed",
                tenant_id=tenant_id,
                kb_slug=kb_slug,
                article_id=article_id,
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=503, detail="embedding service unavailable"
            ) from exc
        except Exception as exc:
            # Defense in depth: a Qdrant outage must not crash the upload.
            log.warning(
                "kb.multimodal.text_upsert_failed",
                tenant_id=tenant_id,
                kb_slug=kb_slug,
                article_id=article_id,
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=503, detail="vector store unavailable"
            ) from exc
```

- [ ] **Step 3: Verify imports + module compiles**

Run:
```bash
cd apps/api && python -c "from knowledge.multimodal.api import router; print('imports ok')"
```
Expected: `imports ok`.

- [ ] **Step 4: Run the failing test — verify it now passes**

Run:
```bash
cd apps/api && pytest tests/knowledge/multimodal/integration/test_multimodal_e2e.py::test_pdf_text_chunks_indexed_to_article_chunks -v
```
Expected: PASS.

- [ ] **Step 5: Run the full multimodal e2e test suite**

Run:
```bash
cd apps/api && pytest tests/knowledge/multimodal/ -v
```
Expected: all tests pass (the new one + all existing ones). If a pre-existing test broke (e.g. a different assertion expected 0 article_chunks calls), reconcile by updating that test rather than reverting the impl.

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/knowledge/multimodal/api.py
git commit -m "feat(multimodal): index PDF text chunks into article_chunks Qdrant collection"
```

---

## Task 3: Verify retrieval works end-to-end

**Files:**
- Modify: `apps/api/tests/knowledge/multimodal/integration/test_multimodal_e2e.py` (append one more test)

- [ ] **Step 1: Append a retrieval test**

Add this test at the end of `test_multimodal_e2e.py`:

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_pdf_text_retrievable_via_search_multimodal_kb(
    tenant_factory: Tenant,
    tmp_path,
) -> None:
    """After PDF upload, the multimodal retriever finds text chunks when
    the user query semantically matches the PDF body.

    Verifies the data is actually queryable (not just written).
    """
    from knowledge.multimodal.retriever import MultimodalRetriever
    from tests.knowledge.multimodal.unit._generate_pdfs import make_table_pdf
    from unittest.mock import AsyncMock, MagicMock, patch
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.http import models as qmodels

    pdf_body = (
        "Reset password instructions: go to settings, click account, "
        "click reset password, and follow the email link."
    )
    pdf_bytes = make_table_pdf(pages=1, body_text=pdf_body)

    # Mock Qdrant to return one match from article_chunks
    fake_query_vector = [0.0] * 1536
    matched_point = MagicMock()
    matched_point.id = "p1"
    matched_point.score = 0.92
    matched_point.payload = {
        "tenant_id": tenant_factory.id,
        "kb_slug": "test-kb",
        "article_id": "a1",
        "source_type": "pdf_text",
        "page_num": 1,
        "text": pdf_body,
    }

    qdrant_mock = MagicMock(spec=AsyncQdrantClient)
    qdrant_mock.query_points = AsyncMock(
        return_value=MagicMock(points=[matched_point])
    )
    qdrant_mock.upsert = AsyncMock()
    qdrant_mock.get_collections = AsyncMock(
        return_value=MagicMock(collections=[])
    )

    fake_image_vector = [0.1] * 1024
    fake_text_vector = [float(len(pdf_body))] + [0.0] * 1535

    async def fake_embed_texts(*, texts, **kwargs):
        return {
            "vectors": [fake_text_vector],
            "model": "fake",
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }

    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(kb_multimodal_router)
    token = create_access_token(
        tenant_id=tenant_factory.id, user_id="admin-1", role="admin"
    )

    with patch("core.qdrant.get_qdrant_client", return_value=qdrant_mock), \
         patch("llm_client.embeddings.embed_texts", side_effect=fake_embed_texts), \
         patch("knowledge.multimodal.embedder.DoubaoVisionEmbedder.encode",
               new=AsyncMock(return_value=MagicMock(
                   vector=fake_image_vector, model="fake"
               ))):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Upload
            upload_resp = await client.post(
                "/api/v1/kb-articles/multimodal",
                params={"kb_slug": "test-kb", "title": "Reset"},
                headers={"Authorization": f"Bearer {token}"},
                files={"file": ("reset.pdf", pdf_bytes, "application/pdf")},
            )
            assert upload_resp.status_code in (200, 201), upload_resp.text

            # Retrieve — query the retriever directly
            retriever = MultimodalRetriever()
            results = await retriever.search(
                tenant_id=tenant_factory.id,
                query="how do I reset my password?",
                query_embedding=fake_query_vector,
                top_k=5,
            )

    assert len(results) >= 1
    top = results[0]
    assert top.payload["source_type"] == "pdf_text"
    assert "password" in top.payload["text"]
```

Adjust the `make_table_pdf` signature to match the existing fixture (it may take `body_text=` as a parameter, or the test may need to use a fixed sample PDF).

- [ ] **Step 2: Run the retrieval test**

Run:
```bash
cd apps/api && pytest tests/knowledge/multimodal/integration/test_multimodal_e2e.py::test_pdf_text_retrievable_via_search_multimodal_kb -v
```
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add apps/api/tests/knowledge/multimodal/integration/test_multimodal_e2e.py
git commit -m "test(multimodal): PDF text chunks are retrievable via MultimodalRetriever"
```

---

## Task 4: Final verification + README update

**Files:**
- Modify: `README.md` (remove tech-debt #18 entry)

- [ ] **Step 1: Run the full non-integration suite**

Run:
```bash
cd apps/api && pytest --collect-only -m "not integration" 2>&1 | tail -5
```
Expected: collection succeeds, 0 errors.

- [ ] **Step 2: Run the multimodal test tree**

Run:
```bash
cd apps/api && pytest tests/knowledge/multimodal/ -v
```
Expected: all pass.

- [ ] **Step 3: Remove tech-debt #18 from README**

Find the tech-debt #18 entry in `README.md` and delete (or strike through) the bullet about "PDF text chunks not indexed to kb_vectors". Confirm:

```bash
grep -n "tech-debt #18\|PDF text chunks" README.md
```
Expected: no match (or only struck-through).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: remove tech-debt #18 entry (PDF text indexing shipped)"
```

---

## Acceptance Checklist

- [ ] PDF uploads write text chunks to the `article_chunks` Qdrant collection.
- [ ] PDF text chunks have `source_type="pdf_text"` + `page_num` + `chunk_index` + `text` in payload.
- [ ] `MultimodalRetriever.search()` returns PDF text chunks in RRF-merged results.
- [ ] Embedding API failures raise HTTP 503 with structured logging (no PII).
- [ ] Qdrant upsert failures raise HTTP 503 with structured logging.
- [ ] `text_chunks_count` DB column accurately reflects the number of chunks indexed (no rename needed).
- [ ] All existing multimodal tests still pass.
