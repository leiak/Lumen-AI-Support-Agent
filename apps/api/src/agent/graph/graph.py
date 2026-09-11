"""Compiled agent graph for the M1 LangChain + LangGraph integration.

Stage 7.1 ships a single linear flow::

    START -> retrieve -> llm -> END

Tools (Stage 7.2+), re-rankers, and checkpointer-backed
multi-turn state (Stage 8+) are deliberately out of scope.
The compiled graph is intended to be invoked *once per AI
turn* via ``await graph.ainvoke({...})`` — there is no
checkpointing and no streaming yet.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from agent.graph.nodes import make_llm_node, make_retrieve_node
from agent.graph.state import AgentState
from knowledge.rag_service import RAGService
from llm_client.client import LLMClient

# Local type alias matching the per-tenant LLMClient factory used
# elsewhere in the agent package.
LLMClientFactory = Callable[[str], LLMClient]


def build_agent_graph(
    *,
    rag_service: RAGService,
    llm_client_factory: LLMClientFactory,
    model: str,
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

    Returns
    -------
    langgraph.graph.state.CompiledStateGraph
        A compiled graph object exposing ``.ainvoke(...)`` and
        ``.invoke(...)``. Stage 7.1 uses ``ainvoke`` only.

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
        "llm",
        make_llm_node(
            llm_client_factory=llm_client_factory,
            model=model,
        ),
    )

    # Edges ----------------------------------------------------------
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "llm")
    graph.add_edge("llm", END)

    return graph.compile()


__all__ = ["build_agent_graph"]
