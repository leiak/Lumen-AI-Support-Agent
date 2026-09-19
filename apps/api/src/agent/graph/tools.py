"""LangChain tools exposed by the M1 agent graph.

This module ships a single tool — ``escalate_to_human`` — for
Stage 7.2. Tools are returned by closure factories that bind
tenant-scoped state from the outer graph state so the LLM cannot
smuggle cross-tenant IDs through tool arguments.

Stage 7.4 — per-turn context dict via ``ContextVar``
---------------------------------------------------

In Stage 7.2 the escalation tool was rebuilt per-turn inside the
LLM node, with ``tenant_id`` + ``conversation_id`` bound by the
factory's constructor kwargs. Stage 7.4 hoists tool construction
out of the per-turn hot path so the graph can build its toolset
once at ``build_agent_graph`` time. With hoisting, the per-turn
``tenant_id`` / ``conversation_id`` are no longer available at
construction; they are only known once ``respond()`` is called.

We solve this with a module-level :class:`contextvars.ContextVar`
that ``SimpleResponder.respond`` writes just before invoking the
graph. The tool's closure reads from that ``ContextVar``, NEVER
from LLM-supplied arguments, so tenant isolation is preserved.

Why ``ContextVar`` (and not a plain ``dict``)?
---------------------------------------------

* ``ContextVar`` is the asyncio-correct primitive for
  per-task ambient context. Each :func:`asyncio.create_task`
  gets its own copy automatically; a plain ``dict`` would
  cross-contaminate concurrent turns.
* It survives across ``await`` boundaries without explicit
  plumbing through every function signature.

Thread-safety caveat
--------------------

``ContextVar`` is per-task in asyncio. FastAPI handles each
request in its own task, so request isolation is structural.
If Stage 7+ adds multi-tenant concurrent graphs in the same
process (e.g. a worker pool running several agents in parallel
under one event loop), each task's ``set`` / ``get`` is
independent and the design still holds. If the codebase ever
moves to threaded workers, the ``ContextVar`` approach will
need revisiting — but M1 has a single asyncio event loop and
we never run agents in worker threads. Stage 7+ should migrate
to ``langgraph.runtime.Runtime`` for the formal binding.

PII contract
------------

The tool never logs ``reason`` / ``summary`` text. Only opaque
IDs and counts go into the structlog payload. The escalation
*message* (what the customer sees) is returned by the tool as
its result content — LangChain turns that into a ``ToolMessage``
the LLM never sees again.
"""
from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Any

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from conversation.service import ConversationService
from core.logging import get_logger

if TYPE_CHECKING:
    from knowledge.rag_service import RAGService
    from knowledge.repository import KnowledgeBaseRepository

log = get_logger(__name__)


# Per-turn escalation context. ``SimpleResponder.respond`` sets
# this BEFORE invoking the graph; the escalation tool's closure
# reads from it. Default sentinel is the empty string so a missed
# ``set`` (e.g. a test that forgets the wiring) fails loudly in
# the tool's body rather than silently binding to ``""``.
_escalation_ctx: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "agent_escalation_ctx", default=("", "")
)


def bind_escalation_context(*, tenant_id: str, conversation_id: str) -> Any:
    """Bind the current asyncio task's escalation context.

    Called by :meth:`SimpleResponder.respond` before invoking the
    graph. Returns a token that the caller MUST pass to
    :func:`reset_escalation_context` after the turn completes —
    this keeps context state from leaking across requests even
    though FastAPI's per-task scheduling already isolates them.

    PII-safe: only opaque ULIDs are stored.
    """
    return _escalation_ctx.set((tenant_id, conversation_id))


def reset_escalation_context(token: Any) -> None:
    """Restore the previous escalation context.

    Called by :meth:`SimpleResponder.respond` after the graph
    completes (success OR failure). Symmetric with
    :func:`bind_escalation_context` — without this, every
    ``respond()`` call would push another context frame and
    the chain would grow unbounded over a long-lived responder.
    """
    _escalation_ctx.reset(token)


def _current_escalation_ids() -> tuple[str, str]:
    """Return ``(tenant_id, conversation_id)`` for the active turn.

    Falls back to the empty-string sentinel if no caller has bound
    the context yet — the tool body treats that as a fatal
    misconfiguration and logs an error.
    """
    return _escalation_ctx.get()


class EscalateArgs(BaseModel):
    """Pydantic schema for the ``escalate_to_human`` tool arguments.

    LangChain's ``@tool`` decorator inspects ``args_schema`` to
    build the JSON Schema sent to the model. ``tenant_id`` and
    ``conversation_id`` are intentionally NOT in this schema —
    they are bound via the module-level ``ContextVar`` rather
    than via the tool's constructor kwargs (Stage 7.4 hoisting).
    """

    reason: str = Field(
        description=(
            "Why this conversation needs a human agent. "
            "Be concise. Customer-facing."
        )
    )
    summary: str | None = Field(
        default=None,
        description=(
            "Optional 1-sentence internal note for the human agent. "
            "Not shown to the customer."
        ),
    )


def make_escalate_tool(
    *,
    conv_service: ConversationService | None = None,
) -> BaseTool:
    """Build a configured ``escalate_to_human`` tool.

    Stage 7.4 — the tool reads ``tenant_id`` / ``conversation_id``
    from the module-level :data:`_escalation_ctx` ``ContextVar``
    instead of from constructor kwargs. This lets
    :func:`agent.graph.graph.build_agent_graph` construct the
    tool ONCE at graph-compile time and reuse it across every
    turn in the responder's lifetime.

    Parameters
    ----------
    conv_service:
        Conversation service. When ``None`` a fresh
        :class:`ConversationService` is constructed; tests pass
        a spy. The tool mutates conversation state exclusively
        through this surface (no direct DB access).

    Returns
    -------
    langchain_core.tools.BaseTool
        A LangChain tool named ``escalate_to_human`` with
        ``ainvoke`` available for the LLM node to dispatch.

    Notes
    -----
    The returned tool's ``ainvoke`` calls
    :meth:`ConversationService.escalate_to_human_queue` — a
    dedicated service method that flips the conversation to
    ``PENDING`` with ``assigned_agent_id=None`` and
    ``ai_handling=False``. We deliberately do NOT call
    :meth:`ConversationService.assign_to_agent` because that
    method's contract is "move the conversation to a SPECIFIC
    agent" (typed ``agent_id: str``); the queue-wait semantic
    of escalation is its own state transition and deserves its
    own service surface.
    """
    if conv_service is None:
        conv_service = ConversationService()

    _bound_conv_service = conv_service

    @tool("escalate_to_human", args_schema=EscalateArgs)
    async def escalate_to_human(
        reason: str,
        summary: str | None = None,
    ) -> dict[str, Any]:
        """Escalate the conversation to a human agent.

        Use this tool when the customer is asking for a human,
        the issue cannot be resolved from the knowledge base,
        or you (the assistant) are explicitly uncertain.

        Returns a small dict the caller can surface to the
        customer. Do NOT include any internal IDs in the
        response.
        """
        tenant_id, conversation_id = _current_escalation_ids()
        if not tenant_id or not conversation_id:
            # No caller has bound the per-turn context. This
            # is a wiring bug — the graph was invoked without
            # ``SimpleResponder.respond`` setting the context
            # first. We log at WARNING (operator-visible) and
            # re-raise so the LLM node falls back to text.
            log.warning(
                "agent.graph.escalation_context_missing",
                error_type="EscalationContextMissing",
            )
            raise RuntimeError(
                "escalate_to_human invoked without a bound escalation context"
            )

        try:
            await _bound_conv_service.escalate_to_human_queue(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )
        except Exception as exc:
            # The tool MUST NOT crash the LLM turn. A failed
            # escalation just means the conversation stays in
            # AI handling — the caller will fall back to the
            # LLM's normal text response. PII-safe log line.
            log.warning(
                "agent.graph.escalation_failed",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                error_type=type(exc).__name__,
            )
            # Re-raise so the llm_node's tool-ainvoke path
            # records the failure and falls back. The fallback
            # contract lives in nodes.py, not here.
            raise

        log.info(
            "agent.graph.escalated_to_human",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            has_summary=1 if summary else 0,
        )
        # Customer-facing payload. The caller turns this into
        # ``state["escalation_message"]``; we don't include
        # ``summary`` (internal-only).
        return {"escalated": True, "reason": reason}

    # The decorator returns a ``BaseTool`` (StructuredTool); the
    # inner ``async def`` is rebound to that tool object so the
    # factory returns the tool directly.
    return escalate_to_human


class SearchInternalKbArgs(BaseModel):
    """Pydantic schema for ``search_internal_kb`` tool arguments."""

    query: str = Field(
        ...,
        description=(
            "Natural-language search query. Will be matched against "
            "the tenant's knowledge base articles via semantic search."
        )
    )
    kb_slug: str | None = Field(
        default=None,
        description=(
            "Optional KB slug to narrow the search. If omitted, "
            "searches across ALL knowledge bases the tenant has access to."
        ),
    )
    top_k: int = Field(
        default=5,
        ge=1,
        le=20,
        description=(
            "Number of chunks to return. Clamped to [1, 20] server-side."
        ),
    )


def make_search_internal_kb_tool(
    *,
    rag_service: "RAGService",
    kb_repository: "KnowledgeBaseRepository",
) -> BaseTool:
    """Build a configured ``search_internal_kb`` tool.

    Closure-injects ``rag_service`` and ``kb_repository`` so the graph
    can build this tool ONCE at compile time (Stage 7.4 hoisting
    pattern). Tenant + conversation ids are read at call time from
    the same ``_escalation_ctx`` ContextVar that
    ``make_escalate_tool`` uses — LLM-supplied args never affect
    tenant isolation.

    PII contract: returns Markdown with article titles + chunk IDs +
    snippet preview. Never logs query text or KB content. Caller (the
    LLM node) decides whether the Markdown flows into a ToolMessage.
    """
    _rag = rag_service
    _kb_repo = kb_repository

    @tool("search_internal_kb", args_schema=SearchInternalKbArgs)
    async def search_internal_kb(
        query: str,
        kb_slug: str | None = None,
        top_k: int = 5,
    ) -> str:
        """Search the tenant's internal knowledge base for relevant articles.

        Use this when:
        - The automatic RAG retrieval didn't surface a relevant article
        - You want to search a specific knowledge base (e.g. billing-only)
        - You need a different angle on the customer's question

        Returns Markdown: top-k chunks with article title + score + snippet.
        """
        tenant_id, conversation_id = _current_escalation_ids()
        if not tenant_id or not conversation_id:
            log.warning(
                "agent.graph.search_internal_kb_context_missing",
                error_type="EscalationContextMissing",
            )
            return "Error: no active conversation context."

        if not query.strip():
            # Guard against an empty / whitespace-only query —
            # saves an embedding round-trip and gives the LLM
            # explicit feedback rather than an empty result set.
            return "Error: empty query."

        kb = None
        if kb_slug:
            # Stage 12 / Task 4 — ``find_by_slug`` is now a real
            # method on :class:`KnowledgeBaseRepository`. The
            # previous ``hasattr`` defensive guard was a Stage-7.4
            # shim while the repo grew the surface; production
            # wiring now guarantees the method exists. A repo
            # implementation that omits it would surface as an
            # ``AttributeError`` immediately rather than silently
            # dropping the slug filter — that's the M2 contract.
            try:
                kb = await _kb_repo.find_by_slug(
                    tenant_id=tenant_id, slug=kb_slug
                )
            except Exception as exc:
                # KB lookup failure must not break the tool — return no KB
                # filter and let RAG search across all tenant KBs.
                # PII-safe payload: kb_slug is intentionally omitted
                # (slugs can carry tenant-meaningful identifiers).
                log.warning(
                    "agent.graph.search_internal_kb_kb_lookup_failed",
                    tenant_id=tenant_id,
                    error_type=type(exc).__name__,
                )
                kb = None

        try:
            # Stage 12 / Task 4 — the tool threads the resolved
            # ``knowledge_base_id`` (from the slug lookup above)
            # directly into ``RAGService.retrieve`` so the RAG
            # service does NOT redo the slug lookup. When the LLM
            # didn't supply a slug, ``kb`` is ``None`` and
            # ``knowledge_base_id=None`` lets ``retrieve`` fan out
            # across the tenant's KBs.
            results = await _rag.retrieve(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                query=query.strip()[:500],
                knowledge_base_id=kb.id if kb else None,
                top_k=max(1, min(top_k, 20)),
            )
        except Exception as exc:
            log.warning(
                "agent.graph.search_internal_kb_retrieve_failed",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                error_type=type(exc).__name__,
            )
            return "Error: knowledge base is temporarily unavailable."

        if not results:
            return "No relevant articles found."

        lines = [f"Found {len(results)} result(s):"]
        for r in results:
            title = r.get("article_title", "Unknown")
            score = r.get("score", 0.0)
            chunk_id = r.get("chunk_id", "")
            content = (r.get("content") or "")[:200]
            lines.append(f"- **{title}** (score={score:.2f}, chunk={chunk_id}): {content}")
        return "\n".join(lines)

    return search_internal_kb


# Public surface for tests / future tool additions.
__all__ = [
    "EscalateArgs",
    "SearchInternalKbArgs",
    "bind_escalation_context",
    "make_escalate_tool",
    "make_search_internal_kb_tool",
    "reset_escalation_context",
]
