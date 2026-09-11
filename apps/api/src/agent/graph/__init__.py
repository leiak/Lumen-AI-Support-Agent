"""LangGraph agent graph for M1 (Stage 7.1).

Stage 7.1 ships the minimal graph scaffolding:

* ``AgentState`` — typed state carried through the graph.
* ``retrieve_node`` — runs RAG against the latest customer turn.
* ``llm_node`` — sends the assembled message list to the LLM.
* ``build_agent_graph`` — composes them into a compiled graph.

The single linear flow is ``START -> retrieve -> llm -> END``.
Tools, re-rankers, and checkpointer-backed sessions land in
later sub-tasks (7.2+).
"""
from __future__ import annotations

from agent.graph.graph import build_agent_graph
from agent.graph.nodes import llm_node, make_llm_node, make_retrieve_node, retrieve_node
from agent.graph.state import AgentState

__all__ = [
    "AgentState",
    "build_agent_graph",
    "llm_node",
    "make_llm_node",
    "make_retrieve_node",
    "retrieve_node",
]
