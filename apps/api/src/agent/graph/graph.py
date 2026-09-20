r"""Compiled agent graph for the M1 LangChain + LangGraph integration.

Stage 7.4 graph topology::

    START -> retrieve -> llm -> conditional
                                  |- escalated=True  -> escalation_node -> END
                                  \- escalated=False -> END

Stage 7.4 changes
-----------------

1. **Tool construction hoisted out of the per-turn hot path.**
   :func:`build_agent_graph` builds the ``escalate_to_human``
   tool ONCE (when ``conv_service`` is provided) and threads it
   into :func:`make_llm_node`. Per-turn ``tenant_id`` +
   ``conversation_id`` are bound via the module-level
   :class:`contextvars.ContextVar` in
   :mod:`agent.graph.tools`, set by :class:`SimpleResponder` at
   the start of each turn.

2. **Per-graph-flow timing metrics.** Each ``ainvoke`` emits a
   pair of ``log.info`` events (``agent.graph.invoke.started``
   / ``agent.graph.invoke.completed``) with ``duration_ms`` and
   ``turn_kind`` (``"rag_hit"`` / ``"no_rag"`` / ``"escalated"``)
   so an operator can grep the structured logs for retrieval /
   LLM latency and the escalation rate.

The compiled graph is intended to be invoked *once per AI turn*
via ``await graph.ainvoke({...})`` — there is no checkpointing
and no streaming yet.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from agent.graph.nodes import (
    make_escalation_node,
    make_llm_node,
    make_retrieve_node,
)
from agent.graph.state import AgentState
from agent.graph.tools import (
    make_escalate_tool,
    make_search_internal_kb_tool,
    make_search_multimodal_kb_tool,
)
from conversation.service import ConversationService
from core.logging import get_logger
from knowledge.rag_service import RAGService
from knowledge.repository import KnowledgeBaseRepository
from llm_client.client import LLMClient
from qdrant_client import AsyncQdrantClient

log = get_logger(__name__)

# Local type alias matching the per-tenant LLMClient factory used
# elsewhere in the agent package.
LLMClientFactory = Callable[[str], LLMClient]

# Node identifiers used by the conditional edge and the routing
# function. Kept at module scope so the LLM-node name and the
# escalation-node name are referenced from exactly one place.
_LLM_NODE = "llm"
_ESCALATION_NODE = "escalation"


def _route_after_llm(state: AgentState) -> str:
    """Return the next node name based on ``state["escalated"]``.

    LangGraph's conditional edge signature is
    ``Callable[[State], str]`` (or ``list[str]`` for parallel
    fan-out). We return the node name to route to; for the False
    branch we return the sentinel ``END`` constant directly —
    LangGraph resolves it against the registered edges.
    """
    if bool(state.get("escalated")):
        return _ESCALATION_NODE
    return END


def _classify_turn_kind(state: AgentState) -> str:
    """Return one of ``"rag_hit"`` / ``"no_rag"`` / ``"escalated"``
    based on the merged post-graph state.

    Used for the per-turn ``log.info`` so an operator can group
    latency / failure metrics by turn kind without parsing
    message content.
    """
    if bool(state.get("escalated")):
        return "escalated"
    rag_messages = state.get("rag_messages") or []
    if rag_messages:
        return "rag_hit"
    return "no_rag"


def build_agent_graph(
    *,
    rag_service: RAGService,
    llm_client_factory: LLMClientFactory,
    model: str,
    conv_service: ConversationService | None = None,
    kb_repository: KnowledgeBaseRepository | None = None,
    qdrant_client: AsyncQdrantClient | None = None,
) -> Any:
    """Build and compile the M1 agent graph.

    Parameters
    ----------
    rag_service:
        Tenant-scoped RAG service. The retrieve node calls
        :meth:`RAGService.build_context_for_query` against the
        latest customer turn. The ``search_internal_kb`` tool
        (Stage 12 / Task 4) calls :meth:`RAGService.retrieve`
        to power LLM-driven on-demand searches.
    llm_client_factory:
        Per-tenant ``LLMClient`` factory. Same factory the
        pre-graph ``SimpleResponder`` used; the LLM node calls
        ``factory(tenant_id)`` to materialize a client for the
        active tenant.
    model:
        Model identifier forwarded to ``LLMClient.chat``.
    conv_service:
        Stage 7.2 — conversation service threaded into the LLM
        node's closure. When provided, the graph hoists
        ``escalate_to_human`` tool construction to graph-build
        time (Stage 7.4). The tool reads its tenant / conversation
        IDs from the module-level ``ContextVar`` bound by
        :class:`SimpleResponder` at the start of each turn.
    kb_repository:
        Stage 12 / Task 4 — knowledge-base repository. When
        supplied (production), the graph hoists the
        ``search_internal_kb`` tool alongside the escalation tool
        so the LLM can advertise and invoke it. Defaults to a
        real :class:`KnowledgeBaseRepository` so production code
        never has to pass it explicitly; tests can pass a mock
        (or omit to bypass the search tool entirely).
    qdrant_client:
        Stage 17 / M2.B Task 6 — Qdrant client. When supplied
        together with ``kb_repository`` (production), the graph
        also hoists the ``search_multimodal_kb`` tool so the LLM
        can advertise and invoke it for image-aware RAG. The
        multimodal tool needs a raw ``AsyncQdrantClient`` because
        its retriever uses ``query_points`` against the
        ``kb_image_vectors`` collection directly. Tests that
        don't exercise multimodal search can omit this parameter
        — the multimodal tool simply isn't registered.

    Returns
    -------
    langgraph.graph.state.CompiledStateGraph
        A compiled graph object exposing ``.ainvoke(...)`` and
        ``.invoke(...)``. Stage 7.4 uses ``ainvoke`` only.

    Notes
    -----
    The return type is :data:`typing.Any` rather than langgraph's
    ``CompiledStateGraph`` because langgraph 1.x's generics are
    not parameterised by usefully-narrow type vars in mypy strict
    mode (the overload picker rejects ``add_node`` calls with our
    plain async-callable node factories). Stage 7.2+ can revisit
    once we move to runtime injection (``langgraph.runtime.Runtime``).
    """
    graph: Any = StateGraph(AgentState)

    # ---- Stage 7.4 + Stage 12 / Task 4: hoist tool construction -----
    # Build the escalation tool ONCE here (when conv_service is
    # provided) and the search_internal_kb tool ONCE here (when
    # kb_repository is provided) so the per-turn hot path doesn't
    # rebuild the LangChain ``@tool`` decorator's StructuredTool
    # each time. Per-turn tenant / conversation IDs flow through
    # the module-level ``ContextVar`` set by SimpleResponder.
    hoisted_tools: list[Any] = []
    if conv_service is not None:
        hoisted_tools.append(
            make_escalate_tool(conv_service=conv_service)
        )
    if kb_repository is not None:
        # Stage 12 / Task 4 — wire the KB-search tool into the
        # production graph so the LLM sees it advertised and can
        # invoke it for on-demand retrieval. The repo + RAG service
        # are the two surfaces the tool needs; both are
        # tenant-isolated at the source (see knowledge.repository /
        # knowledge.rag_service).
        hoisted_tools.append(
            make_search_internal_kb_tool(
                rag_service=rag_service,
                kb_repository=kb_repository,
            )
        )
    if kb_repository is not None and qdrant_client is not None:
        # Stage 17 / M2.B Task 6 — wire the multimodal sibling tool
        # alongside ``search_internal_kb`` so the LLM can choose
        # between text-only RAG and image-aware RRF-fused RAG. The
        # two tools are SIBLINGS, not replacements — see
        # :func:`make_search_multimodal_kb_tool` for why we keep
        # both. Both depend on ``kb_repository`` for slug
        # resolution; the multimodal tool additionally needs the
        # Qdrant client for the ``kb_image_vectors`` collection.
        hoisted_tools.append(
            make_search_multimodal_kb_tool(
                rag_service=rag_service,
                kb_repository=kb_repository,
                qdrant_client=qdrant_client,
            )
        )

    # Nodes ----------------------------------------------------------
    graph.add_node("retrieve", make_retrieve_node(rag_service=rag_service))
    graph.add_node(
        _LLM_NODE,
        make_llm_node(
            llm_client_factory=llm_client_factory,
            model=model,
            tools=hoisted_tools,
        ),
    )
    graph.add_node(_ESCALATION_NODE, make_escalation_node())

    # Edges ----------------------------------------------------------
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", _LLM_NODE)
    # Conditional dispatch: route to the escalation terminal when
    # the LLM fired the tool, otherwise short-circuit straight to
    # END. ``_route_after_llm`` reads ``state["escalated"]``.
    # The mapping dict is mandatory for langgraph >= 0.2 so the
    # runtime can validate the conditional returns against
    # registered node names + the END sentinel.
    graph.add_conditional_edges(
        _LLM_NODE,
        _route_after_llm,
        {
            _ESCALATION_NODE: _ESCALATION_NODE,
            END: END,
        },
    )
    graph.add_edge(_ESCALATION_NODE, END)

    compiled = graph.compile()
    return _wrap_with_metrics(compiled)


def _wrap_with_metrics(compiled: Any) -> Any:
    """Wrap a compiled graph's ``ainvoke`` with timing metrics.

    Stage 7.4 — emits ``agent.graph.invoke.started`` and
    ``agent.graph.invoke.completed`` ``log.info`` events with
    ``duration_ms`` and ``turn_kind`` so an operator can
    compute retrieval / LLM latency and the escalation rate
    from structured logs alone. PII-safe: only opaque IDs and
    counts; never message content.

    Errors raised by ``ainvoke`` propagate (the caller —
    :class:`SimpleResponder` — catches them and returns the
    fallback). We do NOT swallow graph exceptions here; the
    safety-net ``except Exception`` belongs at the node level.
    """
    ainvoke = compiled.ainvoke

    async def _instrumented_ainvoke(
        state: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        tenant_id = state.get("tenant_id") if isinstance(state, dict) else None
        conversation_id = (
            state.get("conversation_id") if isinstance(state, dict) else None
        )
        log.info(
            "agent.graph.invoke.started",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )
        start = time.perf_counter()
        try:
            result = await ainvoke(state, **kwargs)
        except BaseException:
            duration_ms = (time.perf_counter() - start) * 1000.0
            log.warning(
                "agent.graph.invoke.failed",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                duration_ms=round(duration_ms, 3),
            )
            raise
        duration_ms = (time.perf_counter() - start) * 1000.0
        turn_kind: str
        if isinstance(result, dict):
            # Cast to the strict TypedDict only for the classifier;
            # the caller's contract is still a plain dict.
            turn_kind = _classify_turn_kind(result)  # type: ignore[arg-type]
        else:
            turn_kind = "unknown"
        log.info(
            "agent.graph.invoke.completed",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            duration_ms=round(duration_ms, 3),
            turn_kind=turn_kind,
        )
        return result  # type: ignore[no-any-return]

    compiled.ainvoke = _instrumented_ainvoke
    return compiled


__all__ = ["build_agent_graph"]
