"""Compiled agent graph for the M1 LangChain + LangGraph integration.

Stage 7.2 graph topology::

    START -> retrieve -> llm -> conditional
                                  |- escalated=True  -> escalation_node -> END
                                  \- escalated=False -> END

Tools (Stage 7.2 ships ``escalate_to_human`` only) are built
per-turn inside the LLM node using
:func:`agent.graph.tools.make_escalate_tool` — the
``conv_service`` parameter is plumbed through the graph builder
into the LLM node closure so the tool can bind ``tenant_id`` +
``conversation_id`` from the runtime state. The compiled graph
is intended to be invoked *once per AI turn* via
``await graph.ainvoke({...})`` — there is no checkpointing and
no streaming yet.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from agent.graph.nodes import (
    make_escalation_node,
    make_llm_node,
    make_retrieve_node,
)
from agent.graph.state import AgentState
from conversation.service import ConversationService
from knowledge.rag_service import RAGService
from llm_client.client import LLMClient

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


def build_agent_graph(
    *,
    rag_service: RAGService,
    llm_client_factory: LLMClientFactory,
    model: str,
    conv_service: ConversationService | None = None,
) -> Any:
    """Build and compile the M1 agent graph.

    Parameters
    ----------
    rag_service:
        Tenant-scoped RAG service. The retrieve node calls
        :meth:`RAGService.build_context_for_query` against the
        latest customer turn.
    llm_client_factory:
        Per-tenant ``LLMClient`` factory. Same factory the
        pre-graph ``SimpleResponder`` used; the LLM node calls
        ``factory(tenant_id)`` to materialize a client for the
        active tenant.
    model:
        Model identifier forwarded to ``LLMClient.chat``.
    conv_service:
        Stage 7.2 — conversation service threaded into the LLM
        node's closure so the ``escalate_to_human`` tool factory
        can call :meth:`ConversationService.assign_to_agent` at
        runtime. When ``None`` the LLM node defaults to a fresh
        :class:`ConversationService` for the escalation tool.

    Returns
    -------
    langgraph.graph.state.CompiledStateGraph
        A compiled graph object exposing ``.ainvoke(...)`` and
        ``.invoke(...)``. Stage 7.2 uses ``ainvoke`` only.

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

    # Nodes ----------------------------------------------------------
    graph.add_node("retrieve", make_retrieve_node(rag_service=rag_service))
    graph.add_node(
        _LLM_NODE,
        make_llm_node(
            llm_client_factory=llm_client_factory,
            model=model,
            conv_service=conv_service,
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

    return graph.compile()


__all__ = ["build_agent_graph"]
