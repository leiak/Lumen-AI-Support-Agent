"""High-level RAG orchestration for the M1 AI pipeline (Task 6.12).

This module wires :func:`knowledge.retriever.retrieve_chunks` into a
callable that the :class:`agent.simple_responder.SimpleResponder`
invokes on every AI turn. The RAG layer decides:

1. Which knowledge base to use for the tenant (M1: at most one).
2. What ``top_k`` / ``score_threshold`` to apply.
3. How to format retrieved chunks as an LLM-readable system block.

Why a separate service?
-----------------------

The retriever is a pure tenant-scoped vector search; the rag_service
is the policy layer that turns a customer message into "use this KB,
look up this query, format the response". Splitting them lets the
retriever stay reusable for future call sites (admin search,
back-office QA tools, etc.) while keeping the AI auto-reply
integration tight.

Design constraints
------------------

* **RAG failures are non-fatal.** Any error — embedding failure,
  Qdrant down, KB missing, DB hydration glitch — is caught and
  downgraded to an empty :class:`RagContext`. The AI still responds,
  just without retrieval-augmented context. This is critical for M1:
  RAG is an enhancement, not a hard dependency, so a misconfigured
  KB or an OpenAI rate-limit must never take down customer replies.

* **PII discipline.** Logs carry opaque IDs (ULIDs), counts, KB
  name, and the exception class name. NEVER the query text, NEVER
  the chunk text, NEVER the embedding vector.

* **Tenant isolation everywhere.** KB selection, retrieval, and
  chunk formatting all carry ``tenant_id``; a cross-tenant
  retrieval is structurally impossible from this surface.

* **Empty is success.** No KB for the tenant -> empty context.
  No matching content -> empty context. No useful chunk above the
  threshold -> empty context. The caller checks
  ``RagContext.chunk_count == 0`` to decide whether to inject.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.database import get_session
from knowledge.models import KnowledgeBase
from knowledge.repository import KnowledgeBaseRepository
from knowledge.retriever import (
    KnowledgeBaseNotFoundError,
    RetrievedChunk,
    retrieve_chunks,
)
from llm_client.embeddings import EmbeddingError

from core.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class RagContext:
    """A formatted RAG context block ready for injection into an LLM prompt.

    The RAG service produces this and returns it to the caller
    (typically :class:`agent.simple_responder.SimpleResponder`).
    The caller decides what to do with it — for M1 the simple
    responder prepends :attr:`system_message` as a synthetic system
    turn when ``chunk_count > 0``.

    Attributes
    ----------
    system_message:
        Human-readable prompt preamble formatted as
        ``"Retrieved knowledge:\n\n1. ...\n2. ..."``. Empty string
        when no chunks were retrieved.
    chunk_count:
        Number of chunks that contributed to ``system_message``.
        Zero means "no retrieval" — caller should NOT inject.
    knowledge_base_id:
        ULID of the KB that was searched. Empty string when no KB
        was found for the tenant.
    knowledge_base_name:
        Display name of the KB. Empty string when no KB was found.
        Useful for monitoring + debugging but not injected into
        the prompt directly (could carry tenant-meaningful
        identifiers).
    retrieval_score_max:
        Highest cosine similarity among the returned chunks. Zero
        when no chunks. Useful for monitoring retrieval quality
        over time — never logged with chunk text.
    """

    system_message: str
    chunk_count: int
    knowledge_base_id: str
    knowledge_base_name: str
    retrieval_score_max: float


class RAGService:
    """High-level RAG orchestration: tenant → KB selection → retrieval → format.

    Stateless service — one instance can serve the whole process.
    Holds a :class:`KnowledgeBaseRepository` for the KB lookup step;
    :func:`retrieve_chunks` opens its own sessions for retrieval.

    No external dependencies beyond the existing knowledge /
    llm_client layers, so wiring it into :class:`SimpleResponder`
    is a single DI hookup.
    """

    # Default top-k matches the spec's expected answer-window size.
    # Five chunks gives the LLM enough material to pick a confident
    # answer without blowing its context window.
    DEFAULT_TOP_K = 5

    # Cosine similarity floor. Stage 10.3 calibrated this against
    # the live Doubao ``doubao-embedding-vision`` model using
    # ``tests/knowledge/eval/rag_eval_set.json`` (20 synthetic
    # articles, 50 standard + 4 edge-case queries) via
    # ``make eval-rag-real``. The previous 0.3 default was an
    # eyeballed estimate from the text-embedding-3-small era; the
    # threshold-sweep table in ``eval_output_real/eval_report.json``
    # shows 0.45 hits precision=0.72 / recall=0.84 — clean
    # separation between on-topic and out-of-topic chunks without
    # bleeding too much recall. The value still works for
    # text-embedding-3-small (also produces tighter clusters than
    # expected at 0.3); re-run the eval if you switch models.
    DEFAULT_SCORE_THRESHOLD = 0.45

    # Cap the injected context to avoid blowing the LLM's context
    # window. 4000 chars ≈ 1000 tokens which leaves headroom for
    # the system prompt + chat history inside Claude Haiku's
    # comfortably-large context. Truncation appends a note so the
    # LLM knows it saw an incomplete view.
    MAX_CONTEXT_CHARS = 4000

    def __init__(
        self,
        *,
        kb_repo: KnowledgeBaseRepository | None = None,
    ) -> None:
        self._kb_repo = kb_repo or KnowledgeBaseRepository()

    async def build_context_for_query(
        self,
        *,
        tenant_id: str,
        query: str,
        conversation_id: str | None = None,
        knowledge_base_id: str | None = None,
        top_k: int = DEFAULT_TOP_K,
        score_threshold: float | None = DEFAULT_SCORE_THRESHOLD,
    ) -> RagContext:
        """Run RAG for ``query`` and return a formatted context block.

        Parameters
        ----------
        tenant_id:
            Opaque tenant ULID. REQUIRED — every KB lookup and
            retrieval is scoped to this tenant.
        query:
            The customer's latest message (or a synthesized
            retrieval query). Empty / whitespace-only queries
            produce an empty context (no error) — a customer
            sending an empty message shouldn't surface a 500.
        conversation_id:
            Optional, used only for log breadcrumbs so a
            cross-reference between the inbound envelope and the
            RAG lookup is visible in observability tooling. Not
            used for retrieval scoping.
        knowledge_base_id:
            Explicit KB override. When ``None``, the tenant's
            default KB is used (M1: the tenant's only KB; falls
            back to the most recently created if multiple exist).
        top_k:
            Max number of chunks to return. Default
            :attr:`DEFAULT_TOP_K`.
        score_threshold:
            Minimum cosine similarity. Default
            :attr:`DEFAULT_SCORE_THRESHOLD`. Pass ``0.0`` to admit
            every hit; pass ``None`` to disable filtering entirely
            (not recommended for production — irrelevant chunks
            waste context budget).

        Returns
        -------
        :class:`RagContext`
            ``chunk_count == 0`` means no retrieval happened (no
            KB, no chunks, or error). Caller should NOT inject
            an empty context into the LLM prompt.

        Errors
        ------

        Never raises. All error paths produce an empty
        :class:`RagContext` and a WARNING log. This is critical —
        a retrieval failure must NEVER take down the AI auto-reply.
        The LLM still gets the conversation history and can answer
        unaugmented.
        """
        # ---- empty query is a no-op ---------------------------------
        if not query or not query.strip():
            log.info(
                "knowledge.rag.empty_query",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )
            return _empty_context()

        # ---- KB selection ------------------------------------------
        try:
            kb = await self._resolve_kb(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
            )
        except Exception as exc:  # pragma: no cover - defensive
            # DB / connection error during the KB lookup. The AI
            # must still respond, so we downgrade to empty context.
            log.warning(
                "knowledge.rag.kb_lookup_failed",
                tenant_id=tenant_id,
                error_type=type(exc).__name__,
            )
            return _empty_context()

        if kb is None:
            # No KB configured for this tenant. Empty context — the
            # AI just answers without RAG. This is the M1 "fresh
            # tenant, no knowledge base yet" path.
            return _empty_context()

        # ---- retrieve chunks ---------------------------------------
        try:
            chunks = await retrieve_chunks(
                tenant_id=tenant_id,
                knowledge_base_id=kb.id,
                query=query,
                top_k=top_k,
                score_threshold=score_threshold,
            )
        except KnowledgeBaseNotFoundError:
            # Should be impossible — we just looked the KB up — but
            # a concurrent ``delete_kb`` could have wiped the row
            # between our resolve and our retrieve. Swallow as an
            # empty context.
            log.warning(
                "knowledge.rag.kb_vanished",
                tenant_id=tenant_id,
                knowledge_base_id=kb.id,
            )
            return _empty_context()
        except EmbeddingError:
            # ``retrieve_chunks`` re-raises EmbeddingError after a
            # retriever-level breadcrumb. We catch here and
            # downgrade to empty context — the AI still answers.
            log.warning(
                "knowledge.rag.embed_failed",
                tenant_id=tenant_id,
                knowledge_base_id=kb.id,
                error_type=EmbeddingError.__name__,
            )
            return _empty_context()
        except Exception as exc:  # pragma: no cover - defensive
            # Catch-all so an unexpected Qdrant / DB / network
            # glitch NEVER takes down the AI auto-reply. We log
            # the exception class name only (no repr — that can
            # carry infra hints).
            log.warning(
                "knowledge.rag.retrieve_failed",
                tenant_id=tenant_id,
                knowledge_base_id=kb.id,
                error_type=type(exc).__name__,
            )
            return _empty_context()

        if not chunks:
            log.info(
                "knowledge.rag.no_hits",
                tenant_id=tenant_id,
                knowledge_base_id=kb.id,
                top_k=top_k,
            )
            return RagContext(
                system_message="",
                chunk_count=0,
                knowledge_base_id=kb.id,
                knowledge_base_name=kb.name,
                retrieval_score_max=0.0,
            )

        # ---- format + truncate -------------------------------------
        score_max = max((c.score for c in chunks), default=0.0)
        system_message = _format_chunks(chunks=chunks)
        truncated = False
        if len(system_message) > self.MAX_CONTEXT_CHARS:
            system_message = _truncate_with_marker(
                text=system_message,
                limit=self.MAX_CONTEXT_CHARS,
            )
            truncated = True

        log.info(
            "knowledge.rag.context_built",
            tenant_id=tenant_id,
            knowledge_base_id=kb.id,
            chunk_count=len(chunks),
            score_max=score_max,
            truncated=truncated,
        )

        return RagContext(
            system_message=system_message,
            chunk_count=len(chunks),
            knowledge_base_id=kb.id,
            knowledge_base_name=kb.name,
            retrieval_score_max=score_max,
        )

    async def retrieve(
        self,
        *,
        tenant_id: str,
        query: str,
        conversation_id: str | None = None,
        kb_slug: str | None = None,
        knowledge_base_id: str | None = None,
        top_k: int = DEFAULT_TOP_K,
        score_threshold: float | None = DEFAULT_SCORE_THRESHOLD,
    ) -> list[dict[str, Any]]:
        """Run tenant-scoped retrieval and return structured snippets.

        Companion surface to :meth:`build_context_for_query`. Where
        ``build_context_for_query`` returns a pre-formatted Markdown
        block ready for prompt injection, ``retrieve`` returns a
        list of structured snippet dicts so a caller (today: the
        ``search_internal_kb`` LangChain tool) can format them as
        it sees fit.

        Stage 12 / Task 4 — added to close the production-wiring
        gap flagged by the final cross-stage review. The previous
        tool body called ``_rag.retrieve(...)`` which never existed,
        so the LLM never advertised ``search_internal_kb`` at
        runtime. ``retrieve`` is the real seam.

        Each returned dict carries the following keys (PII-safe —
        opaque ULIDs only, never log them; the LLM-facing tool
        already controls PII at the markdown-rendering boundary):

        * ``chunk_id`` (``str``) — the Qdrant point ID; stable across
          re-indexes of the same chunk.
        * ``article_id`` (``str``) — the parent article ULID.
        * ``article_title`` (``str``) — the article's display title;
          useful for the LLM's citation context.
        * ``content`` (``str``) — the chunk text (full, not
          truncated — the caller decides how much to surface).
        * ``score`` (``float``) — the Qdrant cosine similarity;
          higher is more relevant.

        KB scope resolution
        -------------------

        1. ``kb_slug`` provided → look up via
           :meth:`KnowledgeBaseRepository.find_by_slug`. If the slug
           does not exist for this tenant (including cross-tenant),
           return ``[]`` (the tool degrades to "search all KBs" /
           "no KB found" gracefully).
        2. ``knowledge_base_id`` provided → search that single KB
           directly. (The ID is trusted because it flows from the
           repo lookup, not from the LLM.)
        3. Neither provided → fan out across all of the tenant's
           KBs, merging the per-KB result lists into a single
           score-sorted top-k.

        Tenant scope is enforced at TWO layers (defense in depth):

        * The Qdrant ``search_chunks`` helper carries
          ``tenant_id`` + ``knowledge_base_id`` payload filters.
        * The :meth:`ChunkRepository.list_by_point_ids` DB
          hydration also carries ``tenant_id`` in its WHERE clause.

        Parameters
        ----------
        tenant_id:
            Opaque tenant ULID. REQUIRED.
        query:
            Free-form user text. Empty / whitespace-only queries
            return ``[]`` without raising.
        conversation_id:
            Optional log breadcrumb only — does not affect
            retrieval.
        kb_slug:
            Optional slug for KB scoping. When supplied, the
            tenant-scoped KB lookup resolves it to a
            ``knowledge_base_id``; a missing slug returns ``[]``.
        knowledge_base_id:
            Optional explicit KB override. Mutually consistent
            with ``kb_slug`` — when both are supplied,
            ``kb_slug`` wins (more specific).
        top_k:
            Max snippets to return. Default
            :attr:`DEFAULT_TOP_K`. Values <= 0 clamp to 1.
        score_threshold:
            Optional minimum cosine similarity. Default
            :attr:`DEFAULT_SCORE_THRESHOLD`. Pass ``0.0`` /
            ``None`` to admit every hit.

        Returns
        -------
        list[dict[str, Any]]
            Structured snippets sorted by score (highest first),
            capped at ``top_k``. May be empty.

        Notes
        -----
        Never raises on tenant / KB lookup failure — the search
        tool depends on this so the LLM sees "No relevant articles
        found" rather than a hard tool error.
        """
        # ---- empty query is a no-op ---------------------------------
        if not query or not query.strip():
            log.info(
                "knowledge.rag.retrieve_empty_query",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )
            return []

        # Clamp top_k to a sensible positive range; a buggy caller
        # cannot tank the prompt with top_k=-1 or top_k=10**6.
        effective_top_k = max(1, int(top_k))

        # ---- KB scope resolution -----------------------------------
        kb_ids: list[str] | None = None
        resolved_kb: KnowledgeBase | None = None
        if kb_slug is not None:
            try:
                resolved_kb = await self._kb_repo.find_by_slug(
                    tenant_id=tenant_id, slug=kb_slug
                )
            except Exception as exc:  # pragma: no cover - defensive
                log.warning(
                    "knowledge.rag.kb_slug_lookup_failed",
                    tenant_id=tenant_id,
                    error_type=type(exc).__name__,
                )
                resolved_kb = None
            if resolved_kb is None:
                # Slug doesn't exist (or cross-tenant). The tool
                # surfaces this as "No relevant articles found" —
                # same UX as "no hits" but with the slug filter
                # preserved so a probing caller can't enumerate
                # slugs across tenants via response differentiation.
                log.info(
                    "knowledge.rag.kb_slug_not_found",
                    tenant_id=tenant_id,
                    knowledge_base_id=None,
                )
                return []
            kb_ids = [resolved_kb.id]
        elif knowledge_base_id is not None:
            kb_ids = [knowledge_base_id]
        else:
            # Fan out across the tenant's KBs. M1 typically has
            # one KB per tenant, but the multi-KB future is on the
            # M2 roadmap; the implementation already supports it.
            try:
                all_kbs = await self._kb_repo.list_by_tenant(
                    tenant_id=tenant_id, limit=100
                )
            except Exception as exc:  # pragma: no cover - defensive
                log.warning(
                    "knowledge.rag.kb_list_failed",
                    tenant_id=tenant_id,
                    error_type=type(exc).__name__,
                )
                return []
            if not all_kbs:
                return []
            kb_ids = [kb.id for kb in all_kbs]

        # ---- fan-out retrieval across scoped KBs ------------------
        from knowledge.retriever import (
            EmbeddingError as _RetrieverEmbeddingError,
            KnowledgeBaseNotFoundError,
            retrieve_chunks,
        )

        aggregated: list[Any] = []
        per_kb_failures = 0
        for kb_id in kb_ids:
            try:
                chunks = await retrieve_chunks(
                    tenant_id=tenant_id,
                    knowledge_base_id=kb_id,
                    query=query,
                    top_k=effective_top_k,
                    score_threshold=score_threshold,
                )
            except KnowledgeBaseNotFoundError:
                # A concurrent ``delete_kb`` could have wiped the
                # KB between list_by_tenant and retrieve_chunks.
                # Skip silently — the fan-out is best-effort.
                per_kb_failures += 1
                continue
            except _RetrieverEmbeddingError:
                # Embedding layer failure. Skip this KB so a single
                # bad KB doesn't poison the whole turn.
                per_kb_failures += 1
                continue
            except Exception as exc:
                # Catch-all — per-KB failures must not abort the
                # whole fan-out. PII-safe log payload.
                log.warning(
                    "knowledge.rag.retrieve_per_kb_failed",
                    tenant_id=tenant_id,
                    knowledge_base_id=kb_id,
                    error_type=type(exc).__name__,
                )
                per_kb_failures += 1
                continue
            aggregated.extend(chunks)

        if not aggregated:
            return []

        # ---- merge + sort + top-k ----------------------------------
        # Retriever returns ``RetrievedChunk`` objects (highest score
        # first per KB). Merge into a single score-sorted list.
        aggregated.sort(key=lambda c: float(c.score), reverse=True)
        top = aggregated[:effective_top_k]

        # ---- hydrate article titles --------------------------------
        # ``RetrievedChunk`` carries ``article_id`` but not
        # ``article_title``. Bulk-fetch Article rows so each
        # snippet gets a stable, human-readable title.
        title_by_article = await self._article_titles_for(
            tenant_id=tenant_id, article_ids=[t.article_id for t in top]
        )

        snippets: list[dict[str, Any]] = []
        for t in top:
            snippets.append(
                {
                    "chunk_id": t.qdrant_point_id or "",
                    "article_id": t.article_id,
                    "article_title": title_by_article.get(
                        t.article_id, ""
                    ),
                    "content": t.text,
                    "score": float(t.score),
                }
            )

        log.info(
            "knowledge.rag.retrieve_completed",
            tenant_id=tenant_id,
            kb_count=len(kb_ids),
            hit_count=len(snippets),
            per_kb_failures=per_kb_failures,
            score_max=(
                max((s["score"] for s in snippets), default=0.0)
            ),
        )

        return snippets

    async def _article_titles_for(
        self,
        *,
        tenant_id: str,
        article_ids: list[str],
    ) -> dict[str, str]:
        """Bulk-load ``Article.title`` for a list of article IDs.

        Returns a dict keyed by article_id; IDs that don't resolve
        (drift, mid-flight delete) are simply absent from the
        returned map so the caller can fall back to ``""``.
        """
        if not article_ids:
            return {}
        # Local imports so the module remains importable in unit
        # tests that mock the lower-level surfaces.
        from knowledge.models import Article

        async with get_session() as session:
            stmt = select(Article.id, Article.title).where(
                Article.tenant_id == tenant_id,
                Article.id.in_(article_ids),
            )
            rows = list((await session.execute(stmt)).all())
        return {row[0]: (row[1] or "") for row in rows}

    async def _resolve_kb(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str | None,
    ) -> KnowledgeBase | None:
        """Resolve which KB to use for this tenant.

        Returns the ORM :class:`KnowledgeBase` row (or ``None``).
        Caller treats ``None`` as "no KB configured" and skips
        retrieval.

        When ``knowledge_base_id`` is supplied explicitly we use it
        directly. Otherwise we ask the repo for the tenant's
        default KB — M1 rule: the only KB, or most recently
        created if multiple exist.
        """
        if knowledge_base_id is not None:
            kb = await self._kb_repo.get_by_id(
                tenant_id=tenant_id, kb_id=knowledge_base_id
            )
            if kb is None:
                # Explicit override that doesn't exist for this
                # tenant — treat as "no KB" so the caller still
                # gets an AI response. Cross-tenant lookup falls
                # into the same branch (the repo filters by
                # tenant_id).
                log.warning(
                    "knowledge.rag.explicit_kb_missing",
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                )
            return kb

        kb = await self._kb_repo.get_default_for_tenant(tenant_id=tenant_id)
        if kb is None:
            log.info(
                "knowledge.rag.no_kb",
                tenant_id=tenant_id,
            )
        return kb


def _empty_context() -> RagContext:
    """Return a sentinel "no RAG context" :class:`RagContext`.

    Centralized so all the "no retrieval" branches construct the
    same shape — the caller can rely on ``chunk_count == 0`` as
    the single signal.
    """
    return RagContext(
        system_message="",
        chunk_count=0,
        knowledge_base_id="",
        knowledge_base_name="",
        retrieval_score_max=0.0,
    )


def _format_chunks(*, chunks: list[RetrievedChunk]) -> str:
    """Format a list of retrieved chunks into an LLM-readable block.

    Each chunk is rendered as a numbered paragraph with its source
    article id + chunk index so the LLM can cite back if needed.
    The block is intentionally plain text — no markdown — to keep
    the prompt conservative (the LLM doesn't need to render anything).

    The header ``"Retrieved knowledge:"`` primes the LLM to treat
    the block as factual reference rather than instructions; this
    is the same pattern used by most production RAG systems.

    PII: ``chunks[i].chunk.article_id`` is an opaque ULID and is
    safe to surface in the prompt. We do NOT include the article
    title or the KB name — those can carry tenant-meaningful
    identifiers and we don't want them injected into every LLM
    request.
    """
    parts = ["Retrieved knowledge:"]
    for i, chunk in enumerate(chunks, start=1):
        parts.append(f"\n{i}. (article {chunk.article_id}, chunk {chunk.chunk.chunk_index})")
        parts.append(chunk.text)
    return "".join(parts)


def _truncate_with_marker(*, text: str, limit: int) -> str:
    """Truncate ``text`` to at most ``limit`` chars and append a marker.

    Done with a hard slice (not smart word-boundary) so the
    output is deterministic and the LLM never sees malformed
    chunks. The marker tells the model that the retrieval was
    incomplete — Stage 7's re-ranker can use this signal to
    decide whether to do a second pass.
    """
    return text[:limit] + "\n\n[... additional chunks truncated ...]"


__all__ = ["RAGService", "RagContext"]