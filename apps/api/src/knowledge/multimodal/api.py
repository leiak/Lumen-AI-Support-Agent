"""POST /api/v1/kb-articles/multimodal — upload image/PDF, embed, store.

Stage 17 / M2.B Task 6.

Wires the three Task 4/5 modules together:

* :class:`knowledge.multimodal.storage.S3ObjectStore` — durable
  storage in MinIO (dev) or AWS S3 (prod).
* :func:`knowledge.multimodal.embedder.get_vision_embedder` —
  pluggable vision embeddings (Doubao 1024-dim by default; OpenAI
  CLIP 768-dim and Voyage 1024-dim also supported via
  ``settings.vision_provider``). Each provider gracefully degrades
  to zero vectors when its ``*_API_KEY`` is unset.
* :class:`knowledge.multimodal.pdf_processor.PdfProcessor` — text +
  key-page screenshots for PDFs.

Auth + tenant isolation
-----------------------

The endpoint requires a JWT bearer token (same pattern as the M1
``/knowledge-bases/{kb_id}/articles/upload`` endpoint). The tenant
context is sourced from the JWT claim — ``tenant_id`` is NEVER taken
from the request body. This closes the spoofing hole that the plan
sketch's "tenant_id as Form()" would have opened (an attacker who
captured any user's token could upload into a victim tenant).

``kb_slug`` IS a form field (legitimate: the multimodal article
table doesn't have a hard FK to ``knowledge_bases.id`` — it's a
loose reference, same pattern as the plan). The slug is logged at
INFO only.

PII discipline
--------------

Log lines carry opaque IDs only (``article_id``, ``tenant_id``,
``kb_slug``, ``mime_type``, chunk counts, exception class name).
NEVER the file bytes, the file URL with query strings (no S3
presigned URL logging), or the title. Title is customer-meaningful
and never appears in any log payload — operators can join
``kb_multimodal_articles`` to recover it.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from auth.dependencies import get_current_user
from core.config import get_settings
from core.database import get_sessionmaker
from core.id_gen import new_id
from core.logging import get_logger
from core.qdrant import get_qdrant_client
from knowledge.models import KbMultimodalArticle
from knowledge.multimodal.embedder import get_vision_embedder
from knowledge.multimodal.pdf_processor import PdfProcessor
from knowledge.multimodal.storage import ObjectStoreError, get_object_store
from knowledge.qdrant_client import DEFAULT_COLLECTION
from knowledge.startup import ensure_image_collection, get_image_collection_name
from llm_client.embeddings import EmbeddingError, embed_texts
from qdrant_client.models import PointStruct

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1/kb-articles", tags=["kb-multimodal"])

# Hard byte cap. Matches the M1 ``knowledge.parser.MAX_PARSE_BYTES``
# ceiling (50 MiB) for symmetry. A multimodal KB upload larger than
# this is almost certainly a misuse — production would use a real
# media pipeline (S3 multipart + worker) for big files. The cap is
# enforced by an async read loop so we never buffer more than the
# cap into memory.
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Supported MIME types. Order matters for the dispatch below (PDF
# branch is special-cased) but the set itself is what determines
# 400-vs-202 at the API boundary.
_ALLOWED_MIME_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "application/pdf",
    }
)

# Title length cap for the Qdrant payload. The DB column is
# unlimited ``Text`` (see ``KbMultimodalArticle.title``), but the
# Qdrant payload is materialized on every retrieval and the LLM
# only ever reads the title prefix for relevance matching. 200
# chars is plenty for any real title and keeps the payload size
# bounded across millions of points.
QDRANT_TITLE_MAX = 200


async def _read_upload_capped(file: UploadFile, *, cap: int) -> bytes:
    """Read an ``UploadFile`` into memory with a hard byte cap.

    Mirrors the M1 helper in ``knowledge/api.py`` so the two upload
    surfaces share a single DoS-defense shape. Returns the concatenated
    bytes when the file is at or below the cap. Raises
    ``HTTPException(413)`` as soon as the running total would exceed
    the cap.

    PII discipline: thin byte loop — no filenames, no parsed content
    in any log line.
    """
    chunk_size = 1 * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"file exceeds the {cap // (1024 * 1024)} MiB upload limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/multimodal",
    status_code=status.HTTP_201_CREATED,
)
async def upload_multimodal(
    file: Annotated[UploadFile, File(...)],
    kb_slug: Annotated[str, Form(min_length=1, max_length=64)],
    title: Annotated[str, Form(min_length=1, max_length=2000)],
    claims: Annotated[dict[str, Any], Depends(get_current_user)] = None,
) -> dict[str, Any]:
    """Upload image or PDF. Embeds + indexes. Returns article id.

    Status codes:

    * 201 — article created, vectors indexed.
    * 400 — unsupported mime type or missing / empty file.
    * 401 — missing / invalid bearer token.
    * 413 — file exceeds 50 MiB.
    * 503 — S3 or Qdrant unavailable.

    Tech debt (deliberately not addressed in Task 6)
    ------------------------------------------------

    1. PDF text chunks are counted (``text_chunks_count``) but NOT
       indexed into ``article_chunks``. They live only in the
       DB row + Qdrant image vectors (one per key-page screenshot).
       Indexing PDF text into the M1 collection requires extending
       :class:`knowledge.chunker.Chunker` to handle page-aware
       text — tracked for Task 6+.

    2. The endpoint does NOT verify ``kb_slug`` maps to a real
       ``KnowledgeBase`` row. A typo silently indexes into the
       image collection under a wrong slug. Mitigation: log the
       slug at INFO + add a follow-up validation step in a later
       Stage 17+ task.

    3. KB slug is intentionally NOT FK-constrained to
       ``knowledge_bases.slug`` because the M1 schema allows
       deleting a KB without cascade-cleaning multimodal articles.
    """
    if claims is None:  # pragma: no cover - Depends always wins
        raise HTTPException(401, "missing bearer token")
    tenant_id: str = claims["tenant_id"]

    mime_type = file.content_type or ""
    if mime_type not in _ALLOWED_MIME_TYPES:
        # PII: the mime type is not customer content (it's a protocol
        # value the client selected), so logging it is safe.
        log.info(
            "kb.multimodal.unsupported_mime",
            tenant_id=tenant_id,
            mime_type=mime_type,
        )
        raise HTTPException(
            status_code=400,
            detail=f"unsupported mime type: {mime_type}",
        )

    file_bytes = await _read_upload_capped(file, cap=_MAX_UPLOAD_BYTES)
    if not file_bytes:
        raise HTTPException(status_code=400, detail="uploaded file is empty")

    article_id = new_id()

    # 1. Upload to object store. The key is tenant-prefixed
    #    (defense-in-depth: a misconfigured IAM policy can't make
    #    the app exfiltrate cross-tenant data).
    # ``os.path.basename`` strips any directory components the
    # client may have smuggled into the filename — without it an
    # attacker could craft a multipart filename like
    # ``../../../etc/passwd`` and have it become a path segment
    # in the S3 key. ``or "upload"`` handles the empty-string
    # case where the client doesn't supply a filename at all.
    safe_filename = os.path.basename(file.filename or "upload") or "upload"
    storage_key = f"{tenant_id}/{kb_slug}/{article_id}/{safe_filename}"
    try:
        # S3ObjectStore.put is sync; offload to a thread so the event
        # loop isn't blocked on the network round-trip.
        store = get_object_store()
        file_url = await asyncio.to_thread(
            store.put,
            storage_key,
            file_bytes,
            mime_type,
        )
    except ObjectStoreError as exc:
        log.warning(
            "kb.multimodal.storage_failed",
            tenant_id=tenant_id,
            kb_slug=kb_slug,
            article_id=article_id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="object store unavailable") from exc
    except Exception as exc:
        # Catch-all: boto3 can raise a wide zoo of ClientError
        # subclasses — we don't want to enumerate them all. PII-safe
        # log line carries only the exception class name.
        log.warning(
            "kb.multimodal.storage_error",
            tenant_id=tenant_id,
            kb_slug=kb_slug,
            article_id=article_id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="object store unavailable") from exc

    # 2. Process based on type — extract text chunks + key-page screenshots
    if mime_type == "application/pdf":
        processor = PdfProcessor()
        extracted = processor.extract(file_bytes)
        text_chunks_n = len(extracted.text_chunks)
        screenshots = list(extracted.screenshot_pages)
    else:
        # Image — single "chunk" using the file bytes directly.
        text_chunks_n = 0
        screenshots = [(1, file_bytes)]

    # 3. Vision embed each screenshot. The embedder owns its own
    #    httpx client — we MUST aclose() it before returning.
    settings = get_settings()
    embedder = get_vision_embedder(settings)
    image_collection = get_image_collection_name(settings.vision_provider)
    try:
        image_vectors: list[tuple[int, list[float]]] = []
        for page_num, png_bytes in screenshots:
            # Mime type matters: the data URI we send to Doubao
            # is ``data:{mime_type};base64,...`` and the model
            # uses it to dispatch the right decoder. For PDF
            # key-page screenshots the bytes are real PNG, so
            # ``image/png`` is correct. For direct image uploads
            # (JPEG / WebP / PNG) we use the client-supplied
            # ``file.content_type`` — previously this was
            # hardcoded to ``image/png`` which produced a
            # type-mismatched data URI for non-PNG uploads.
            if mime_type == "application/pdf":
                emb_mime = "image/png"
            else:
                emb_mime = file.content_type or "application/octet-stream"
            emb = await embedder.encode(
                png_bytes,
                mime_type=emb_mime,
            )
            image_vectors.append((page_num, emb.vector))
    finally:
        await embedder.aclose()

    # 3.5. Resolve the Qdrant client once. Both the text-chunk
    #      upsert (3.6 below) and the image-vector upsert
    #      (section 4) need it; resolving it here avoids a second
    #      singleton lookup in section 4.
    qdrant = get_qdrant_client()

    # 3.6. Index PDF text chunks into the article_chunks collection
    #      (tech debt #18). Mirrors the M1 worker
    #      (:func:`knowledge.worker.index_article`) payload keys
    #      (``tenant_id``, ``kb_slug``, ``article_id``) plus
    #      ``source_type="pdf_text"`` + ``page_num`` + ``chunk_index``
    #      so the MultimodalRetriever's existing RRF merge over
    #      ``DEFAULT_COLLECTION`` + ``kb_image_vectors`` returns
    #      PDF text in the same result set as regular KB articles.
    text_chunks: list[str] = (
        list(extracted.text_chunks) if mime_type == "application/pdf" else []
    )
    text_chunk_pages: list[int] = (
        list(extracted.text_chunk_pages) if mime_type == "application/pdf" else []
    )
    if text_chunks:
        # Invariant from pdf_processor: text_chunks and text_chunk_pages are
        # appended in lockstep, but we runtime-check defensively so a future
        # edit to the processor doesn't silently truncate or shift.
        if len(text_chunks) != len(text_chunk_pages):
            raise RuntimeError(
                "pdf_processor invariant violated: "
                f"text_chunks and text_chunk_pages lengths differ "
                f"({len(text_chunks)} vs {len(text_chunk_pages)})"
            )

        # Embed the text chunks (may raise EmbeddingError or downstream API errors).
        try:
            embed_result = await embed_texts(
                texts=text_chunks,
                tenant_id=tenant_id,
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
            for idx, (page_num, text, vec) in enumerate(
                zip(text_chunk_pages, text_chunks, embed_result.vectors)
            )
        ]

        # Upsert to Qdrant (separate try block so non-EmbeddingError upstream
        # errors are not mislabelled as vector-store failures).
        try:
            await qdrant.upsert(
                collection_name=DEFAULT_COLLECTION,
                points=text_points,
                wait=True,
            )
        except Exception as exc:
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

    # 4. Insert image vectors into the kb_image_vectors Qdrant
    #    collection. We ensure the collection exists inside the
    #    handler too (defense against a fresh Qdrant after a
    #    restart that lost the bootstrap window).
    ok = await ensure_image_collection(
        qdrant, dimension=embedder.dimension
    )
    if not ok:
        log.warning(
            "kb.multimodal.image_collection_unavailable",
            tenant_id=tenant_id,
            kb_slug=kb_slug,
            article_id=article_id,
        )
        raise HTTPException(
            status_code=503, detail="vector store unavailable"
        )

    if image_vectors:
        points = [
            PointStruct(
                id=new_id(),
                vector=vec,
                payload={
                    "tenant_id": tenant_id,
                    "kb_slug": kb_slug,
                    "article_id": article_id,
                    "page_num": page_num,
                    "mime_type": (
                        "image/png"
                        if mime_type == "application/pdf"
                        else mime_type
                    ),
                    "source_type": "image",
                    # ``title`` is intentionally stored in the
                    # payload so a future LLM-facing tool can
                    # surface the article title without a DB join.
                    # It carries PII risk (customer text), so we
                    # accept the trade-off because the alternative
                    # — a join on every retrieval — would dominate
                    # the latency budget. The Qdrant MUST-filter
                    # still prevents cross-tenant reads.
                    # Truncated to :data:`QDRANT_TITLE_MAX` to
                    # keep the payload size bounded — see the
                    # constant's docstring for rationale.
                    "title": title[:QDRANT_TITLE_MAX],
                },
            )
            for page_num, vec in image_vectors
        ]
        try:
            await qdrant.upsert(
                collection_name=image_collection,
                points=points,
                wait=True,
            )
        except Exception as exc:
            log.warning(
                "kb.multimodal.upsert_failed",
                tenant_id=tenant_id,
                kb_slug=kb_slug,
                article_id=article_id,
                point_count=len(points),
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=503, detail="vector store unavailable"
            ) from exc

    # 5. Insert DB row (the durable record of what we just uploaded).
    sm = get_sessionmaker()
    async with sm() as session:
        article = KbMultimodalArticle(
            id=article_id,
            tenant_id=tenant_id,
            kb_slug=kb_slug,
            title=title,
            file_url=file_url,
            mime_type=mime_type,
            file_size_bytes=len(file_bytes),
            text_chunks_count=text_chunks_n,
            image_chunks_count=len(screenshots),
        )
        session.add(article)
        await session.commit()

    log.info(
        "kb.multimodal.uploaded",
        tenant_id=tenant_id,
        kb_slug=kb_slug,
        article_id=article_id,
        mime_type=mime_type,
        text_chunks=text_chunks_n,
        image_chunks=len(screenshots),
    )

    return {
        "article_id": article_id,
        # ``text_chunks`` is the count of text chunks the PDF
        # processor EXTRACTED from the document. These are
        # persisted in the DB row + ``text_chunks_count`` column
        # for future use, but they are NOT yet indexed into the
        # M1 ``article_chunks`` Qdrant collection — Task 6
        # deliberately defers that to a later task that will
        # extend the chunker for page-aware text. ``image_chunks``
        # is the count of vectors that WERE indexed into the
        # ``kb_image_vectors`` Qdrant collection (one per PDF
        # key-page screenshot, or one for direct image uploads).
        # Names reflect "extracted" vs "indexed" so the operator
        # can tell which step succeeded from the response alone.
        "text_chunks": text_chunks_n,  # extracted (not yet indexed)
        "image_chunks": len(screenshots),  # indexed in kb_image_vectors
    }


__all__ = ["router"]
