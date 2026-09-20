# Tech Debt #18 — PDF Text Chunks Indexed to article_chunks

> **Status:** design approved (M3 kickoff). Implementation plan: `docs/superpowers/plans/2026-09-20-tech-debt-18-pdf-text-indexing.md` (TBD).

## Goal

Embed PDF text chunks (extracted by `PdfProcessor`) and upsert them to the existing `article_chunks` Qdrant collection so the agent graph can retrieve text from PDFs in a single `search_kb` / `search_multimodal_kb` call alongside regular text articles.

## Context

`apps/api/src/knowledge/multimodal/api.py:upload_multimodal` (lines 122–387) currently:
1. Calls `PdfProcessor.extract(file_bytes)` which already produces `text_chunks: list[tuple[page_num, text]]` (see `pdf_processor.py:43–100`)
2. Only the page screenshots go through `DoubaoVisionEmbedder.encode()` and into `kb_image_vectors`
3. The PDF's text content is discarded — never embedded, never searchable

The `Chunker` class (`apps/api/src/knowledge/chunker.py`) was deliberately scoped to plain-text articles; the M2.B in-source TODO at `multimodal/api.py:142–161` says: *"Indexing PDF text into the M1 collection requires extending `:class:knowledge.chunker.Chunker` to handle page-aware text — tracked for Task 6+."*

A second in-source comment notes: PDFs are page-bounded, so reusing the plain-text `Chunker` (which splits on token count) would mix pages. We must keep PDF page metadata attached to each chunk.

## Approach

**Embed PDF text via the same `embed_texts` path used by `index_article`** (`apps/api/src/knowledge/worker.py:429`), but call it **synchronously** in the upload handler (the existing Arq index queue is for plain-text article re-indexing; multimodal uploads already do their image embedding synchronously, so adding text embedding is symmetric).

### File-level changes

#### `apps/api/src/knowledge/multimodal/api.py`

In `upload_multimodal`, after the existing screenshot-embedding block (lines 242–270) and before the `kb_image_vectors` upsert (lines 326–330), add a new block:

```python
# Stage 18 / M3 / tech-debt #18: index PDF text chunks into the
# unified article_chunks collection so the agent graph can retrieve
# PDF text via search_kb alongside regular text articles. Mirrors
# knowledge/worker.py:index_article payload keys (tenant_id, kb_slug,
# article_id) plus source_type=pdf_text + page_num for filtering.
if text_chunks:  # empty list for image-only PDFs
    from knowledge.qdrant_client import DEFAULT_COLLECTION
    from llm_client.embeddings import embed_texts

    chunk_texts = [text for _page, text in text_chunks]
    chunk_vectors = await embed_texts(
        texts=chunk_texts,
        tenant_id=tenant_id,
    )
    await qdrant_client.upsert(
        collection_name=DEFAULT_COLLECTION,
        points=[
            PointStruct(
                id=str(ulid.new()),
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
            in enumerate(zip(text_chunks, chunk_vectors))
        ],
    )
```

`text_chunks_n` (line 355) — already persisted on the DB row — stays as-is; it now reflects "what we indexed", not "what we discarded". The companion `image_chunks_count` column already exists on `KbMultimodalArticle` (`knowledge/models.py:295–298`) and is unaffected by this change.

#### `apps/api/src/knowledge/multimodal/retriever.py`

No code change. The existing `MultimodalRetriever` already queries `DEFAULT_COLLECTION` plus `IMAGE_COLLECTION` via RRF (k=60); adding PDF text points to `DEFAULT_COLLECTION` automatically makes them part of the unified retrieval set.

#### `apps/api/src/agent/graph/tools.py:search_multimodal_kb`

No code change. The 1024-dim placeholder for `image_query_embedding` already works; text PDFs don't need a query-side image embedding.

### Test changes

#### `apps/api/tests/knowledge/multimodal/integration/test_multimodal_e2e.py`

Add `test_pdf_text_chunks_indexed_to_article_chunks`:
1. POST a 2-page PDF (use the existing `_generate_pdfs.py:make_table_pdf`)
2. Mock `embed_texts` to return deterministic 1024-dim vectors (one per chunk)
3. Mock Qdrant `upsert` to capture `points`
4. Assert at least one call to `upsert(collection_name="article_chunks", ...)`
5. Assert the points' payloads have `source_type="pdf_text"`, the expected `page_num` range, and matching `tenant_id` / `kb_slug` / `article_id`
6. Assert `len(points) == len(text_chunks)` for the sample PDF

#### `apps/api/tests/knowledge/multimodal/unit/test_pdf_processor.py`

Add `test_text_chunks_carry_page_numbers` — verifies `text_chunks` is a list of `(page_num, text)` tuples with strictly increasing `page_num` starting at 1 (sanity check on the page-aware chunking contract).

### Performance note

Upload latency grows from `~500ms` (image embedding only) to `~3s` (images + N text-embedding API calls). Acceptable because:
- The current upload path already does synchronous image embedding; this is symmetric
- PDF uploads are infrequent (admin/manual) vs. agent chat (per-message) — request path is not affected
- The `embed_texts` batching (default 32 texts/batch) keeps this linear in chunk count

## Multi-tenant isolation

PDF text payloads carry `tenant_id`; the existing `MultimodalRetriever` already filters on `tenant_id` at query time (`retriever.py` lines 50–60, 80–90). No change.

## PII discipline

No change. PDF content is customer-uploaded knowledge, not customer chat messages — logged only as opaque `article_id` / `kb_slug` counts.

## Out of scope

- Reindexing existing PDFs that were uploaded before this change (only applies to new uploads)
- Persisting `Chunk` DB rows for PDF text (mirrors the image-only path's Qdrant-only choice)
- Page-level keyword search on PDF text (vector retrieval only)

## Acceptance

- [ ] PDF uploads write text chunks to `article_chunks` Qdrant collection.
- [ ] PDF text chunks have `source_type="pdf_text"` and `page_num` in payload.
- [ ] `MultimodalRetriever.search()` returns PDF text chunks in RRF-merged results when relevant.
- [ ] Existing multimodal e2e tests still pass.
- [ ] Upload latency increases linearly with `len(text_chunks)`, capped by `embed_texts` batching.
