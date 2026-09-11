"""Tenant-scoped vector retrieval for the knowledge base (Task 6.11).

The retriever is the read side of the RAG pipeline: it embeds a free-form
query, runs a tenant-scoped similarity search against the Qdrant
``article_chunks`` collection, then hydrates the matching ``Chunk`` rows
from the DB so callers (the answer-generation stage, future chat
endpoints, etc.) get a self-contained ``RetrievedChunk`` with the full
chunk text + metadata.

Design constraints
------------------

* **Tenant isolation at two layers.** Qdrant search carries a
  ``tenant_id`` + ``knowledge_base_id`` payload filter (the
  ``search_chunks`` helper enforces this — there is no un-scoped
  search surface). The DB read also includes ``tenant_id`` in the
  WHERE clause so a Qdrant/DB drift incident can't silently leak
  cross-tenant data into a response.

* **PII discipline.** Logs carry opaque IDs (ULIDs), counts, and the
  exception class name. NEVER the query text (user input — may carry
  PII), NEVER the chunk text, NEVER the embedding vector.

* **Async all the way.** Embedding, Qdrant search, and the DB
  hydration are all awaited; no blocking calls anywhere.

* **Empty result is success.** ``retrieve_chunks`` returns ``[]`` on
  no hits. Only structural problems raise (``EmbeddingError``,
  ``KnowledgeBaseNotFoundError``, ``ValueError`` on empty query).

* **Score preservation.** The retriever preserves Qdrant's score
  ordering (highest score first). After hydration we re-sort only
  when ``score_threshold`` drops some hits — the surviving list is
  still in score order because we filter (not re-order) on threshold.

Why hydrate from DB at all?
---------------------------

The worker writes the same payload to BOTH Qdrant and the ``chunks``
table. Qdrant is the similarity search index; the ``chunks`` table is
the source of truth for the full chunk row (text, token_count,
metadata_json, FKs to article/article_version). Returning
``RetrievedChunk`` with the full ORM row means callers get stable
references (no Qdrant re-fetch needed), and a future move to a
different vector store doesn't have to keep the payload schema in
sync.
"""
from __future__ import annotations

from dataclasses import dataclass

from knowledge.models import Chunk, KnowledgeBase
from knowledge.qdrant_client import DEFAULT_COLLECTION, search_chunks
from knowledge.repository import ChunkRepository, KnowledgeBaseRepository
from llm_client.embeddings import EmbeddingError, embed_texts

from core.logging import get_logger

log = get_logger(__name__)


class KnowledgeBaseNotFoundError(ValueError):
    """Raised when the requested ``knowledge_base_id`` does not exist for the tenant.

    Subclasses ``ValueError`` so callers that already handle generic
    bad-input still treat this correctly — but a caller that wants to
    distinguish "KB missing" from "query empty" can catch this first.
    """


@dataclass(slots=True)
class RetrievedChunk:
    """A chunk returned from retrieval, with score + DB row hydrated.

    Attributes
    ----------
    chunk:
        The SQLAlchemy ORM row hydrated from the ``chunks`` table.
        Carries ``text``, ``token_count``, ``metadata_json``, and the
        FK columns the caller might want.
    score:
        Qdrant cosine similarity score (0..1 for cosine, since the
        worker uses the cosine distance metric; could be negative
        for other metrics — callers should treat it as "higher is
        more similar").
    article_id, knowledge_base_id, tenant_id:
        Denormalized onto the dataclass for convenience. These are
        redundant with ``chunk.article_id`` / ``chunk.knowledge_base_id``
        / ``chunk.tenant_id`` — the redundancy exists so a caller
        doesn't have to reach through the ORM row to do per-tenant
        grouping in the common case.
    """

    chunk: Chunk
    score: float
    article_id: str
    knowledge_base_id: str
    tenant_id: str

    @property
    def text(self) -> str:
        """Convenience accessor for ``chunk.text``."""
        return self.chunk.text

    @property
    def qdrant_point_id(self) -> str | None:
        """Convenience accessor for ``chunk.qdrant_point_id``."""
        return self.chunk.qdrant_point_id


async def _assert_kb_exists(
    *, tenant_id: str, knowledge_base_id: str
) -> KnowledgeBase:
    """Look up the KB and return its row, raising on missing/cross-tenant.

    Uses ``KnowledgeBaseRepository.get_by_id`` which already does the
    tenant-scoped lookup. A cross-tenant request — KB exists for
    tenant A, caller asks as tenant B — returns ``None`` and we raise
    the same exception; that's intentional, a cross-tenant lookup
    should look identical to a missing KB to the caller.

    Returns the loaded ``KnowledgeBase`` ORM row (not just a bool) so
    the caller can reuse it — historically :func:`retrieve_chunks`
    loaded the row here AND re-loaded it via ``session.get`` a few
    lines later to read ``embedding_model``, costing two identical
    roundtrips per retrieval. Returning the row collapses both reads
    into one. See the note in :func:`retrieve_chunks` for the full
    rationale.

    We check KB existence BEFORE the embedding call so a misrouted
    call (wrong KB id, stale reference) doesn't waste an OpenAI
    request on a query that can't possibly succeed.
    """
    repo = KnowledgeBaseRepository()
    kb = await repo.get_by_id(tenant_id=tenant_id, kb_id=knowledge_base_id)
    if kb is None:
        raise KnowledgeBaseNotFoundError(
            f"knowledge_base {knowledge_base_id!r} not found for tenant {tenant_id!r}"
        )
    return kb


async def retrieve_chunks(
    *,
    tenant_id: str,
    knowledge_base_id: str,
    query: str,
    top_k: int = 5,
    score_threshold: float | None = None,
) -> list[RetrievedChunk]:
    """Tenant-scoped vector retrieval over the ``article_chunks`` collection.

    Parameters
    ----------
    tenant_id:
        Opaque tenant ULID. REQUIRED — every Qdrant filter and DB
        WHERE clause carries this. A missing or wrong value is a
        cross-tenant leak surface; this function intentionally takes
        no default.
    knowledge_base_id:
        Opaque KB ULID scoped to ``tenant_id``. The retriever
        raises :class:`KnowledgeBaseNotFoundError` if no KB with
        this id exists for the tenant — a missing KB and a
        cross-tenant KB lookup both look like "not found" by design.
    query:
        Free-form user text. Will be embedded via
        :func:`embed_texts`. MUST be non-empty — empty queries are a
        caller bug, not a valid retrieval target, and raise
        ``ValueError`` so the bug surfaces in tests.
    top_k:
        Max number of chunks to return. Default ``5`` matches the
        spec's expected answer-window size. ``top_k <= 0`` raises
        ``ValueError``.
    score_threshold:
        Optional minimum cosine similarity (Qdrant returns the raw
        cosine similarity score in [0, 1] when the collection uses
        the cosine distance metric). Hits below the threshold are
        dropped. When ``None``, every hit returned by Qdrant is
        included.

    Returns
    -------
    list[RetrievedChunk]
        Sorted highest-score-first. May be empty.

    Raises
    ------
    ValueError:
        ``query`` is empty / whitespace-only or ``top_k <= 0``.
        Subclass :class:`KnowledgeBaseNotFoundError` is raised for a
        missing or cross-tenant KB; callers that want to distinguish
        the two should catch that one first.
    KnowledgeBaseNotFoundError:
        The supplied ``knowledge_base_id`` does not exist for
        ``tenant_id``. Raised BEFORE the embedding call so a bad
        caller doesn't burn an OpenAI request.
    EmbeddingError:
        The query embedding failed (rate-limit exhausted, auth, 4xx,
        etc.). We re-raise after logging only the exception class
        name — see "PII discipline" below.

    PII discipline
    --------------

    Log lines carry ``tenant_id``, ``knowledge_base_id`` (both opaque
    ULIDs), ``top_k``, ``hit_count``, ``model``, and ``error_type``.
    NEVER the query text (user input), NEVER the chunk text, NEVER
    the embedding vector.
    """
    # ---- input validation ---------------------------------------------
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string")
    if top_k <= 0:
        raise ValueError(f"top_k must be positive, got {top_k}")

    # ---- KB existence check + pinned-model lookup --------------------
    # Done before embedding so a misrouted call doesn't burn a request
    # against OpenAI. Cheap DB roundtrip, semantically the first thing
    # a tenant-scoped API should do.
    #
    # Optimization: ``_assert_kb_exists`` returns the loaded KB row, so
    # we can read ``embedding_model`` off it directly without a second
    # ``session.get(KnowledgeBase, ...)`` roundtrip. The pinned model
    # is per-KB so the same query embedded against KB-A and KB-B
    # doesn't yield comparable vectors — the retriever MUST honor the
    # KB's pinned model (Stage 7+ re-rankers can compare across
    # models). The row is detached but ``expire_on_commit=False`` on
    # the sessionmaker (see ``core.database.get_sessionmaker``) keeps
    # the column values readable after the assertion call returns.
    kb = await _assert_kb_exists(
        tenant_id=tenant_id, knowledge_base_id=knowledge_base_id
    )
    embedding_model = kb.embedding_model

    # ---- embed the query --------------------------------------------
    try:
        result = await embed_texts(
            texts=[query],
            model=embedding_model,
            tenant_id=tenant_id,
        )
    except EmbeddingError:
        # embed_texts already logs the full failure at WARNING;
        # we add a retriever-specific breadcrumb (just the class
        # name — no exception text, which can carry OpenAI keys /
        # infra hints).
        log.warning(
            "knowledge.retrieve.embed_failed",
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            error_type=EmbeddingError.__name__,
        )
        raise

    if not result.vectors:
        # Defensive — embed_texts returns vectors==[] only when the
        # input list was empty; we already validated ``query``, so
        # this branch should be unreachable. Surface as an explicit
        # error rather than silently returning [] (which would
        # mask the misconfiguration).
        raise ValueError("embed_texts returned no vectors for a non-empty query")

    query_vector = result.vectors[0]

    # ---- Qdrant search (tenant + KB filter) --------------------------
    scored_points = await search_chunks(
        collection=DEFAULT_COLLECTION,
        query_vector=query_vector,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        top_k=top_k,
    )

    if not scored_points:
        log.info(
            "knowledge.retrieve.empty",
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            top_k=top_k,
        )
        return []

    # ---- hydrate Chunk rows ------------------------------------------
    point_ids = [str(p.id) for p in scored_points]
    chunks_by_pid = await ChunkRepository().list_by_point_ids(
        tenant_id=tenant_id,
        point_ids=point_ids,
    )

    # ---- assemble final list, preserving Qdrant order ---------------
    # We walk the Qdrant hits in their native order so the result is
    # sorted highest-score-first. We skip hits whose Chunk row is
    # missing (Qdrant / DB drift) so an orphan vector doesn't surface
    # to the caller. After the threshold filter the list is still
    # in score order because we filter (not re-order).
    retrieved: list[RetrievedChunk] = []
    skipped_orphans = 0
    for hit in scored_points:
        score = float(hit.score)
        if score_threshold is not None and score < score_threshold:
            continue
        pid = str(hit.id)
        chunk_row = chunks_by_pid.get(pid)
        if chunk_row is None:
            skipped_orphans += 1
            continue
        retrieved.append(
            RetrievedChunk(
                chunk=chunk_row,
                score=score,
                article_id=chunk_row.article_id,
                knowledge_base_id=chunk_row.knowledge_base_id,
                tenant_id=chunk_row.tenant_id,
            )
        )

    log.info(
        "knowledge.retrieve.success",
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        top_k=top_k,
        hit_count=len(retrieved),
        model=embedding_model,
        skipped_orphans=skipped_orphans,
    )

    return retrieved


__all__ = [
    "KnowledgeBaseNotFoundError",
    "RetrievedChunk",
    "retrieve_chunks",
]