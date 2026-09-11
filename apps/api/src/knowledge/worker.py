"""Async indexer worker for the knowledge base ingestion pipeline.

Task 6.7 (M1): runs the parse → chunk → embed → upsert pipeline for a
single ``Article`` + its current ``ArticleVersion``.

Pipeline
--------

1. Load ``Article`` + its ``current_version_id`` ``ArticleVersion`` +
   the parent ``KnowledgeBase``.
2. Transition ``Article.status`` to ``INDEXING`` (committed).
3. Build a synthetic :class:`knowledge.parser.ParsedDocument` from
   ``raw_text``. The full file → bytes path will be wired in 6.10 (file
   upload API); for M1 we index whatever the version already holds.
4. Extract structured blocks via :func:`knowledge.multimodal.enrich_blocks`.
5. Chunk via :func:`knowledge.chunker.chunk_document` using the KB's
   ``chunk_size`` / ``chunk_overlap``.
6. Embed chunk texts via :func:`llm_client.embeddings.embed_texts`.
7. Upsert vectors to the Qdrant collection
   (:data:`knowledge.qdrant_client.DEFAULT_COLLECTION`).
8. Persist ``Chunk`` rows with the backfilled ``qdrant_point_id``.
9. Transition ``Article.status`` to ``INDEXED``, clear
   ``error_message``.

On any failure after step 2 the article transitions to ``FAILED`` with
``error_message`` set to the exception class name ONLY (never the raw
error text — error bodies can carry PII, file paths, or infra hints).

Design constraints
------------------

* **Tenant isolation** — every Qdrant payload carries ``tenant_id`` so
  the 6.11 retriever can scope queries without a join. The worker
  itself does not query by ``tenant_id`` because it operates on a
  single ``article_id`` that already implies tenant scope.
* **Idempotency** — Qdrant point IDs are deterministic
  (``chunk-<ULID>``). Re-running the worker for the same article
  upserts the same points. Existing points for the
  ``article_version_id`` are deleted BEFORE upsert so chunk-count
  drift is handled cleanly.
* **DB-side idempotency** — existing ``Chunk`` rows for the version
  are deleted BEFORE the new ones are inserted. ``(article_version_id,
  chunk_index)`` is ``UNIQUE``, so the alternative (upsert by that
  key) would need an extra round-trip to find existing IDs; delete +
  insert is simpler and the chunk count is small.
* **No raw error text in DB / logs.** ``Article.error_message``
  stores ONLY ``type(exc).__name__``. The full ``repr(exc)`` is logged
  separately at WARNING level for operator debug — never returned to
  the caller in any user-facing surface.
* **PII-safe logs.** Logs carry opaque IDs (ULIDs), counts, status
  names, and ``error_type``. NEVER raw text, chunk text, error
  messages, or embedding vectors.
* **Async all the way.** Public entry point is ``async def``; uses
  ``async with get_session()`` for every DB transaction.

M1 scope
--------

This module is the async function the dispatcher calls. The
``arq`` / ``redis`` / ``celery`` worker wrapper is a Stage 7+ concern.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from qdrant_client.http import models as qmodels
from sqlalchemy import delete

from core.database import get_session
from core.logging import get_logger
from knowledge.chunker import chunk_document
from knowledge.enums import ArticleStatus
from knowledge.models import (
    Article,
    ArticleVersion,
    Chunk,
    KnowledgeBase,
)
from knowledge.multimodal import enrich_blocks
from knowledge.parser import ParsedDocument
from knowledge.qdrant_client import (
    DEFAULT_COLLECTION,
    delete_points_by_article_version,
    upsert_chunks,
)
from llm_client.embeddings import embed_texts

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Stable namespace for Qdrant point ID derivation. Using a fixed URL
# keeps the UUID5 deterministic across processes / restarts — every
# ``_qdrant_point_id(chunk_id)`` call produces the SAME UUID for the
# SAME chunk ULID, which is the whole point of the determinism
# contract. The namespace itself is arbitrary but must not change
# (changing it would silently re-map every existing point).
_POINT_ID_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")


def _chunk_id(article_version_id: str, chunk_index: int) -> str:
    """Compute a deterministic chunk primary key.

    The ``chunks.id`` column is ``VARCHAR(26)`` (ULID length) — the
    schema deliberately stays narrow so the PK index is compact.
    UUID5 hex (32 chars) does NOT fit; we use SHA-256 over the
    ``(article_version_id, chunk_index)`` pair and truncate the
    hex digest to 26 chars. SHA-256 gives 128 bits of collision
    resistance, so 104 bits (26 hex chars) is more than enough to
    keep (version, index) pairs unique across all tenants.

    Determinism guarantee: the SAME (article_version_id,
    chunk_index) ALWAYS yields the SAME 26-char ID, across processes
    and restarts. That's what makes the worker idempotent at the
    Chunk + Qdrant layer.
    """
    payload = f"chunk-v:{article_version_id}:{chunk_index}".encode()
    return hashlib.sha256(payload).hexdigest()[:26]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IndexResult:
    """The outcome of a single ``index_article`` call.

    Attributes
    ----------
    chunks_indexed:
        Number of chunk rows persisted + Qdrant points upserted. ``0``
        when the article was empty / produced no chunks (still marked
        ``INDEXED`` — an empty document is a valid indexed state).
    status:
        The final :class:`knowledge.enums.ArticleStatus`. ``INDEXED``
        on success, ``FAILED`` on any caught exception. ``INDEXING``
        is never returned — the worker always transitions out of it.
    """

    chunks_indexed: int
    status: ArticleStatus


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def index_article(*, article_id: str) -> IndexResult:
    """Run the full indexing pipeline for one article.

    Determinism / idempotency
    -------------------------

    Qdrant point IDs are derived from each chunk's ``Chunk.id`` ULID
    via :func:`uuid.uuid5` with a fixed namespace, producing a
    canonical UUID string. UUID5 is deterministic — the SAME chunk
    ULID always maps to the SAME UUID, across processes and restarts,
    which is the contract the 6.11 retriever relies on for upsert
    idempotency.

    We use UUID (not a custom string) because the Qdrant server's
    point ID validator only accepts unsigned integers or UUIDs on
    older server versions (e.g. 1.13.x). The newer client (>= 1.14)
    accepts arbitrary strings, but the server-side constraint is what
    governs the choice here.

    Before the upsert we DELETE any existing Qdrant points with the
    same ``article_version_id`` payload (see
    :func:`delete_points_by_article_version`). This handles the case
    where the chunk count shrank between runs (e.g. chunker parameter
    change).

    On the DB side we DELETE existing ``Chunk`` rows for the
    ``article_version_id`` before INSERTing the new ones. The
    alternative — upsert by ``(article_version_id, chunk_index)`` —
    would require an extra round-trip to find existing row IDs and
    adds little value for an M1 worker that runs one-at-a-time.

    Failure handling
    ----------------

    On ANY exception after the article has been moved to ``INDEXING``,
    we transition to ``FAILED`` and store ONLY the exception class
    name in ``error_message``. The full ``repr(exc)`` is logged
    separately at WARNING level so operators have a debug trail
    without exposing PII or infra hints in the database.

    A failure in the FINAL status update is swallowed + ERROR-logged
    because we already did the work — a flaky commit must not mask a
    successful index.
    """
    # Step 1. Load article + version + KB.
    async with get_session() as session:
        article = await session.get(Article, article_id)
        if article is None:
            log.warning(
                "knowledge.index.article_missing",
                article_id=article_id,
            )
            # No status to update — the article doesn't exist. Treat
            # as FAILED with a clear class name; the dispatcher can
            # decide to surface this to the caller.
            return IndexResult(chunks_indexed=0, status=ArticleStatus.FAILED)

        kb = await session.get(KnowledgeBase, article.knowledge_base_id)
        if kb is None:
            # Should be impossible thanks to the FK CASCADE, but be
            # defensive: surface a FAILED transition.
            log.error(
                "knowledge.index.kb_missing",
                article_id=article_id,
                knowledge_base_id=article.knowledge_base_id,
            )
            return await _fail_and_return(
                article_id=article_id,
                exc=ValueError("knowledge_base_missing"),
                chunks_indexed=0,
            )

        if article.current_version_id is None:
            return await _fail_and_return(
                article_id=article_id,
                exc=ValueError("article_missing_current_version"),
                chunks_indexed=0,
            )

        version = await session.get(ArticleVersion, article.current_version_id)
        if version is None:
            # FK should prevent this; if the version got deleted out
            # from under us, fail cleanly.
            return await _fail_and_return(
                article_id=article_id,
                exc=ValueError("article_version_missing"),
                chunks_indexed=0,
            )

        # Capture the values we need OUTSIDE the session because the
        # load happens in its own transaction; subsequent steps open
        # fresh sessions.
        tenant_id = article.tenant_id
        knowledge_base_id = article.knowledge_base_id
        version_id = version.id
        raw_text = version.raw_text
        embedding_model = kb.embedding_model
        chunk_size = kb.chunk_size
        chunk_overlap = kb.chunk_overlap

    # Step 2. Transition to INDEXING (own session, committed).
    transitioned = await _set_status(article_id, ArticleStatus.INDEXING)
    if not transitioned:
        # Couldn't even update status — give up rather than run
        # the heavy pipeline without a clear lifecycle marker.
        log.error(
            "knowledge.index.status_transition_failed",
            article_id=article_id,
            target_status=ArticleStatus.INDEXING.value,
        )
        return IndexResult(chunks_indexed=0, status=ArticleStatus.FAILED)

    # Steps 3-9 wrapped in a try/except so any failure funnels into
    # the FAILED state. We re-bind a few locals here so the except
    # block can use them.
    try:
        # Step 3. Synthetic ParsedDocument from raw_text. M1: we
        # index whatever the ArticleVersion holds. The 6.10 upload
        # API will replace this with a real parse_document() call
        # fed by file_bytes + file_name + mime_type.
        parsed = ParsedDocument(
            text=raw_text,
            blocks=[],
            metadata={
                "source_format": "text",  # M1: raw_text is always plain text
                "char_count": len(raw_text),
            },
            format="text",
        )

        # Step 4. Multimodal block extraction. For M1 the only
        # useful branch is markdown (which won't fire on plain
        # ``raw_text``); kept for parity with the 6.10 wiring.
        try:
            blocks = await enrich_blocks(parsed=parsed)
        except Exception as exc:
            # Multimodal failures are non-fatal — we still have
            # the linearized text. Log + continue with [].
            log.warning(
                "knowledge.index.enrich_blocks_failed",
                article_id=article_id,
                error_type=type(exc).__name__,
            )
            blocks = []
        parsed.blocks = blocks

        # Step 5. Chunk.
        chunk_candidates = await chunk_document(
            parsed=parsed,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

        log.info(
            "knowledge.index.chunks_ready",
            article_id=article_id,
            article_version_id=version_id,
            chunk_count=len(chunk_candidates),
        )

        # Edge case: empty document. We mark it INDEXED with 0
        # chunks — an empty article is a valid indexed state and
        # the retriever simply returns no results for it.
        if not chunk_candidates:
            # Delete any stale Qdrant points + Chunk rows from a
            # previous non-empty run (defense in depth — usually
            # a no-op).
            await delete_points_by_article_version(
                collection=DEFAULT_COLLECTION,
                article_version_id=version_id,
            )
            async with get_session() as session:
                await session.execute(
                    delete(Chunk).where(Chunk.article_version_id == version_id)
                )
                await session.commit()
            await _set_status(
                article_id,
                ArticleStatus.INDEXED,
                clear_error_message=True,
            )
            log.info(
                "knowledge.index.empty_article",
                article_id=article_id,
                article_version_id=version_id,
            )
            return IndexResult(chunks_indexed=0, status=ArticleStatus.INDEXED)

        # Step 6. Embed. ``embed_texts`` is imported at module
        # level so tests can monkeypatch
        # ``knowledge.worker.embed_texts`` directly.
        texts = [c.text for c in chunk_candidates]
        embedding_result = await embed_texts(
            texts=texts,
            model=embedding_model,
            tenant_id=tenant_id,
        )

        if len(embedding_result.vectors) != len(texts):
            # Defensive — OpenAI guarantees input order, but a
            # mid-stack failure or a future Stage 7+ provider could
            # in principle mis-align. Fail loudly.
            raise ValueError(
                f"embedding vector count {len(embedding_result.vectors)} "
                f"does not match input text count {len(texts)}"
            )

        # Step 6b. Allocate Chunk.id ULIDs + compute Qdrant point
        # IDs deterministically. We do this BEFORE the upsert so we
        # can pass the IDs through to the Qdrant payload AND
        # back-fill them onto the Chunk rows.
        #
        # The chunk.id is derived from (article_version_id,
        # chunk_index) via UUID5 — not a fresh ULID — so re-running
        # ``index_article`` produces the SAME chunk.id and therefore
        # the SAME Qdrant point ID. The whole point of the
        # delete-then-insert idempotency contract.
        chunk_rows: list[Chunk] = []
        qdrant_points: list[qmodels.PointStruct] = []
        for candidate, vector in zip(
            chunk_candidates, embedding_result.vectors, strict=True
        ):
            chunk_id = _chunk_id(version_id, candidate.chunk_index)
            point_id = _qdrant_point_id(chunk_id)
            metadata = _sanitize_metadata(candidate.metadata)
            payload: dict[str, Any] = {
                "tenant_id": tenant_id,
                "knowledge_base_id": knowledge_base_id,
                "article_id": article_id,
                "article_version_id": version_id,
                "chunk_index": candidate.chunk_index,
                "text": candidate.text,
                "metadata": metadata,
            }
            qdrant_points.append(
                qmodels.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=payload,
                )
            )
            chunk_rows.append(
                Chunk(
                    id=chunk_id,
                    article_version_id=version_id,
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    article_id=article_id,
                    chunk_index=candidate.chunk_index,
                    text=candidate.text,
                    token_count=candidate.token_count,
                    metadata_json=metadata,
                    qdrant_point_id=point_id,
                )
            )

        # Step 7a. Clean stale Qdrant points (defense in depth
        # against chunk-count drift). Done BEFORE the upsert so we
        # don't accidentally remove the freshly-upserted points.
        await delete_points_by_article_version(
            collection=DEFAULT_COLLECTION,
            article_version_id=version_id,
        )

        # Step 7b. Upsert.
        upserted = await upsert_chunks(
            collection=DEFAULT_COLLECTION,
            points=qdrant_points,
        )
        if upserted != len(qdrant_points):
            # upsert_chunks returned 0 (or a partial count). Treat
            # as a hard failure so the article goes to FAILED and an
            # operator can retry.
            raise RuntimeError(
                f"qdrant upsert returned {upserted}, expected {len(qdrant_points)}"
            )

        # Step 8. Persist Chunk rows. Delete-then-insert keeps the
        # UNIQUE (article_version_id, chunk_index) constraint happy
        # without an extra round-trip to find existing IDs.
        async with get_session() as session:
            await session.execute(
                delete(Chunk).where(Chunk.article_version_id == version_id)
            )
            for chunk_row in chunk_rows:
                session.add(chunk_row)
            await session.commit()

        # Step 9. Transition to INDEXED, clear error_message.
        await _set_status(
            article_id,
            ArticleStatus.INDEXED,
            clear_error_message=True,
        )

        log.info(
            "knowledge.index.done",
            article_id=article_id,
            article_version_id=version_id,
            chunk_count=len(chunk_rows),
            embedding_model=embedding_model,
        )
        return IndexResult(
            chunks_indexed=len(chunk_rows),
            status=ArticleStatus.INDEXED,
        )

    except Exception as exc:
        return await _fail_and_return(
            article_id=article_id,
            exc=exc,
            chunks_indexed=0,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ``error_type_only`` — we deliberately store ONLY the exception
# class name in Article.error_message. The full repr is logged at
# WARNING level so operators can debug without exposing PII / infra
# hints in the database.
def _qdrant_point_id(chunk_id: str) -> str:
    """Compute a deterministic Qdrant point ID from a chunk ULID.

    Qdrant (server >= 1.13) accepts unsigned integers or UUIDs as
    point IDs. We map the chunk ULID through ``uuid.uuid5`` with a
    fixed namespace so the SAME chunk ULID always produces the SAME
    UUID — this is what makes ``index_article`` idempotent at the
    Qdrant layer.

    The ULID itself is a 26-char string; UUID5 of a string yields a
    canonical 36-char UUID string (8-4-4-4-12 with hyphens). That's
    exactly what Qdrant's serializer accepts on every supported
    server version.
    """
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, f"chunk:{chunk_id}"))


def _sanitize_metadata(
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Coerce chunk metadata values into JSON-safe primitives.

    Qdrant's payload schema (JSONB on the Postgres side, JSON on the
    gRPC/REST wire) rejects non-JSON types. Anything that isn't a
    ``str`` / ``int`` / ``float`` / ``bool`` / ``None`` / ``list`` /
    ``dict`` of those is coerced via ``str(...)``. We never want a
    malformed payload to crash the worker.
    """
    out: dict[str, Any] = {}
    for key, value in metadata.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, (list, dict)):
            out[key] = _sanitize_metadata(value) if isinstance(value, dict) else [
                _safe_primitive(v) for v in value
            ]
        else:
            out[key] = str(value)
    return out


def _safe_primitive(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


async def _set_status(
    article_id: str,
    status: ArticleStatus,
    *,
    error_message: str | None = None,
    clear_error_message: bool = False,
) -> bool:
    """Set ``Article.status`` (and optionally ``error_message``).

    Parameters
    ----------
    article_id, status:
        The article to update and the new lifecycle status.
    error_message:
        When ``not None``, the value to store on
        ``Article.error_message``. Typically only set when transitioning
        to ``FAILED`` — the value is the exception class name (NEVER the
        raw error text, which can carry PII / infra hints).
    clear_error_message:
        When True, ``Article.error_message`` is wiped to ``NULL``.
        Mutually exclusive in spirit with ``error_message`` — prefer
        this flag when transitioning to ``INDEXED`` so a previous
        ``FAILED`` attempt doesn't leave stale diagnostic text on the
        successful run.

    Returns
    -------
    True on a successful commit, False if the DB call raised.

    We deliberately swallow DB errors here because we don't want a
    follow-up failure to mask the original indexing failure; the
    caller already returned an ``IndexResult`` based on the work it
    was able to do.
    """
    try:
        async with get_session() as session:
            article = await session.get(Article, article_id)
            if article is None:
                log.warning(
                    "knowledge.index.status_target_missing",
                    article_id=article_id,
                    target_status=status.value,
                )
                return False
            article.status = status
            if clear_error_message:
                article.error_message = None
            elif error_message is not None:
                article.error_message = error_message
            await session.commit()
            return True
    except Exception as exc:
        log.error(
            "knowledge.index.status_update_error",
            article_id=article_id,
            target_status=status.value,
            error_type=type(exc).__name__,
        )
        return False


async def _fail_and_return(
    *,
    article_id: str,
    exc: BaseException,
    chunks_indexed: int,
) -> IndexResult:
    """Common failure path: log full repr, persist class name, return FAILED.

    Steps:

    1. Emit a WARNING log with the FULL repr (PII-safe because
       ``repr(exc)`` only carries exception class + args; the
       underlying message is the caller's responsibility to keep
       short and non-PII).
    2. Persist ONLY ``type(exc).__name__`` to ``Article.error_message``.
    3. Transition ``Article.status`` to ``FAILED``.
    4. Return ``IndexResult`` with ``status=FAILED``.
    """
    error_type = type(exc).__name__

    # 1. Operator-facing log: carries full repr for debug.
    log.warning(
        "knowledge.index.failed",
        article_id=article_id,
        error_type=error_type,
        error_repr=repr(exc),
    )

    # 2 + 3. Persist class name + FAILED status. We use
    # _set_status with the explicit error_message so the column is
    # updated atomically with the status change. We swallow any DB
    # failure here — see _set_status for the rationale.
    await _set_status(
        article_id,
        ArticleStatus.FAILED,
        error_message=error_type,
    )

    return IndexResult(chunks_indexed=chunks_indexed, status=ArticleStatus.FAILED)
