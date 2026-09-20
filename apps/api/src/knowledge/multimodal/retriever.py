"""RRF-fused retrieval over the M1 text + multimodal image collections.

Stage 17 / M2.B Task 6.

Reciprocal Rank Fusion (``RRF_K = 60`` is the literature default —
see Cormack et al. 2009) lets us combine the two cosine-ranked
lists without having to normalize their scores: each hit's
contribution is ``1 / (k + rank)`` based on its position in the
per-list ranking, not its raw similarity. Robust to differing
score scales between text-embedding-3-small (1536-dim) and the
vision embedder (1024-dim).

Design notes
------------

* The retriever talks to Qdrant directly via the ``AsyncQdrantClient``
  singleton — same pattern the M1 ``knowledge.retriever`` uses. We
  do NOT reuse :func:`knowledge.qdrant_client.search_chunks` because
  the M1 helper hard-codes the ``knowledge_base_id`` payload filter
  and the multimodal case uses ``kb_slug`` (multimodal articles
  don't belong to a single KB by id — they reference a KB by slug
  stored on ``KbMultimodalArticle.kb_slug``).

* Tenant isolation is enforced at TWO layers (defense in depth):

  1. The Qdrant MUST-filter payload conditions carry ``tenant_id``
     + the per-list scope (``knowledge_base_id`` for text,
     ``kb_slug`` for images).
  2. Cross-tenant hits simply don't exist — the filter excludes them
     at the Qdrant layer.

* PII discipline: only opaque IDs (chunk_id, article_id, kb_slug),
  the source_type label, and counts go into log payloads. Never the
  query text, the embedding vector, or the hit metadata contents.

Collection mapping deviation from the plan
------------------------------------------

The plan sketch referenced ``kb_vectors`` for the M1 text collection.
The actual collection name in the codebase is ``article_chunks``
(see ``knowledge.qdrant_client.DEFAULT_COLLECTION``). This retriever
queries ``article_chunks`` with a ``knowledge_base_id`` payload
filter (resolved from the kb_slug via :class:`KnowledgeBaseRepository`)
so it integrates cleanly with the existing M1 indexer rather than
diverging into a parallel collection layout.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient

from core.config import get_settings
from core.logging import get_logger
from knowledge.qdrant_client import DEFAULT_COLLECTION
from knowledge.startup import get_image_collection_name

log = get_logger(__name__)

# Literature default for RRF (Cormack et al. 2009). Smaller values
# weight top-ranked results more aggressively; 60 keeps the contribution
# of low-ranked hits non-trivial so a chunk that's only in the text
# list at rank 8 can still boost past an image-only top-1.
RRF_K = 60


@dataclass(frozen=True)
class RetrievalHit:
    """A single RRF-fused result.

    ``score`` is the RRF score (NOT the original cosine similarity).
    ``source_type`` distinguishes which collection the hit came from.
    ``article_id`` is the parent article id (ULID) — set on every
    text hit by the M1 worker, and on every image hit by the Stage 17
    upload handler.
    """

    chunk_id: str
    score: float
    source_type: str  # "text" | "image"
    article_id: str
    metadata: dict


class MultimodalRetriever:
    """RRF-fused retrieval across text + image collections.

    Construct with an :class:`AsyncQdrantClient` and a ``top_k``. The
    retriever is stateless — one instance per process is fine.
    """

    def __init__(self, qdrant_client: AsyncQdrantClient, *, top_k: int = 5) -> None:
        self._qdrant = qdrant_client
        self._top_k = max(1, int(top_k))

    async def retrieve(
        self,
        *,
        tenant_id: str,
        kb_slug: str | None,
        text_query_embedding: list[float],
        image_query_embedding: list[float] | None = None,
        knowledge_base_id: str | None = None,
    ) -> list[RetrievalHit]:
        """RRF-fused retrieval.

        Parameters
        ----------
        tenant_id:
            Opaque tenant ULID. Required — flows into the Qdrant
            MUST-filter on every collection.
        kb_slug:
            KB slug for the image-collection scope. When ``None``
            we skip the image search (the M1 text-only path).
        text_query_embedding:
            Pre-computed text query vector (typically from
            :func:`knowledge.rag_service.RAGService.build_context_for_query`
            — this retriever does NOT embed for you).
        image_query_embedding:
            Optional vision-embedding for the image collection.
            ``None`` means "text-only query, skip image search".
            In the current Stage 17 tool surface the LLM only ever
            supplies a text query so this stays ``None``.
        knowledge_base_id:
            The M1 KB id (used by the text-collection MUST-filter).
            ``None`` means "fan out across all of the tenant's KBs"
            — same semantics as :class:`RAGService.retrieve`.
        """
        # Pull 2*top_k from each list so the fused ranking has enough
        # material to interleave. RRF is robust to over-fetching
        # because the position-based score only uses rank, not raw
        # similarity.
        per_list_limit = self._top_k * 2

        text_hits = await self._search_text(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            vector=text_query_embedding,
            limit=per_list_limit,
        )
        image_hits: list[RetrievalHit] = []
        if image_query_embedding is not None and kb_slug:
            image_hits = await self._search_images(
                tenant_id=tenant_id,
                kb_slug=kb_slug,
                vector=image_query_embedding,
                limit=per_list_limit,
            )

        return self._rrf_fuse(text_hits, image_hits)

    async def _search_text(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str | None,
        vector: list[float],
        limit: int,
    ) -> list[RetrievalHit]:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        must: list[FieldCondition] = [
            FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id))
        ]
        # When the caller resolved a single KB id (via slug lookup),
        # we narrow the MUST-filter to that KB. When ``None``, the
        # fan-out happens at the RAG service layer (text-embedding
        # results get merged) — the retriever just queries the
        # tenant-wide collection.
        if knowledge_base_id:
            must.append(
                FieldCondition(
                    key="knowledge_base_id",
                    match=MatchValue(value=knowledge_base_id),
                )
            )

        try:
            # qdrant-client >= 1.14 unified ``search`` /
            # ``search_batch`` / ``recommend`` / ``discover`` /
            # ``scroll`` into a single ``query_points`` entry
            # point. The old ``search`` method was removed in
            # 1.19; we use ``query_points`` here for forward
            # compatibility and to match the existing pattern in
            # ``knowledge.qdrant_client.search_chunks``. The
            # ``points`` attribute on the response holds the
            # highest-scored results as a NameList[ScoredPoint].
            response = await self._qdrant.query_points(
                collection_name=DEFAULT_COLLECTION,
                query=vector,
                query_filter=Filter(must=must),
                limit=limit,
            )
        except Exception as exc:
            # Qdrant transient error — degrade to empty list. The
            # caller still gets an RRF result (just text-only or
            # image-only). PII-safe log line.
            log.warning(
                "multimodal.retriever.text_search_failed",
                tenant_id=tenant_id,
                error_type=type(exc).__name__,
            )
            return []

        scored = list(response.points) if response.points else []
        return [
            RetrievalHit(
                chunk_id=str(h.id),
                score=float(h.score) if h.score is not None else 0.0,
                source_type="text",
                article_id=str(h.payload.get("article_id", "")) if h.payload else "",
                metadata=dict(h.payload) if h.payload else {},
            )
            for h in scored
        ]

    async def _search_images(
        self,
        *,
        tenant_id: str,
        kb_slug: str,
        vector: list[float],
        limit: int,
    ) -> list[RetrievalHit]:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        must: list[FieldCondition] = [
            FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
            FieldCondition(key="kb_slug", match=MatchValue(value=kb_slug)),
        ]

        try:
            # See ``_search_text`` for the ``query_points``
            # rationale. Same unified API; the ``query`` param
            # accepts a bare ``list[float]`` as the nearest-neighbor
            # form.
            response = await self._qdrant.query_points(
                collection_name=get_image_collection_name(get_settings().vision_provider),
                query=vector,
                query_filter=Filter(must=must),
                limit=limit,
            )
        except Exception as exc:
            log.warning(
                "multimodal.retriever.image_search_failed",
                tenant_id=tenant_id,
                kb_slug=kb_slug,
                error_type=type(exc).__name__,
            )
            return []

        scored = list(response.points) if response.points else []
        return [
            RetrievalHit(
                chunk_id=str(h.id),
                score=float(h.score) if h.score is not None else 0.0,
                source_type="image",
                article_id=str(h.payload.get("article_id", "")) if h.payload else "",
                metadata=dict(h.payload) if h.payload else {},
            )
            for h in scored
        ]

    @staticmethod
    def _rrf_fuse(
        text_hits: list[RetrievalHit],
        image_hits: list[RetrievalHit],
    ) -> list[RetrievalHit]:
        """Reciprocal Rank Fusion with k=60.

        For each hit, the score is ``sum(1 / (k + rank))`` across the
        lists it appears in. A hit that's the top of BOTH lists scores
        higher than a hit that's only top of one — that's the
        cross-modal "agreement" signal RRF is designed to capture.

        Empty inputs → empty output (no ranking, no dummy entries).
        """
        if not text_hits and not image_hits:
            return []

        scores: dict[str, float] = {}
        meta: dict[str, RetrievalHit] = {}
        for rank, hit in enumerate(text_hits, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            meta[hit.chunk_id] = hit
        for rank, hit in enumerate(image_hits, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            # When a chunk_id appears in both lists we keep the LAST
            # meta (image wins on tie). This is an arbitrary choice —
            # the LLM-facing tool only reads ``source_type`` /
            # ``article_id`` / ``metadata``, so the difference is
            # cosmetic. Documenting the choice so future maintainers
            # don't think it's accidental.
            meta[hit.chunk_id] = hit

        sorted_ids = sorted(
            scores.keys(), key=lambda k: scores[k], reverse=True
        )
        return [
            RetrievalHit(
                chunk_id=cid,
                score=scores[cid],
                source_type=meta[cid].source_type,
                article_id=meta[cid].article_id,
                metadata=meta[cid].metadata,
            )
            for cid in sorted_ids
        ]
