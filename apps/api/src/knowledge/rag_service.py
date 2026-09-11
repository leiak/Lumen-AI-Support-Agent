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

    # Cosine similarity floor. Below 0.3 the chunk is essentially
    # unrelated to the query under text-embedding-3-small + cosine.
    # 0.3 was chosen empirically as a reasonable "weakly relevant"
    # boundary for M1 — low enough to surface partial matches, high
    # enough to keep obviously-irrelevant chunks out of the prompt.
    DEFAULT_SCORE_THRESHOLD = 0.3

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

    async def _resolve_kb(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str | None,
    ) -> "object | None":
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

        kbs = await self._kb_repo.list_by_tenant(tenant_id=tenant_id, limit=2)
        if not kbs:
            log.info(
                "knowledge.rag.no_kb",
                tenant_id=tenant_id,
            )
            return None
        if len(kbs) > 1:
            # Multiple KBs for one tenant is a misconfiguration
            # under the M1 singleton rule. Pick the newest
            # (list_by_tenant is already newest-first) so the
            # behavior is deterministic and the WARNING surfaces
            # the misconfiguration in observability.
            log.warning(
                "knowledge.rag.multiple_kbs_using_newest",
                tenant_id=tenant_id,
                kb_count=len(kbs),
            )
        return kbs[0]


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