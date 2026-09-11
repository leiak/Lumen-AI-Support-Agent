"""Typed state carried through the LangGraph agent graph.

The graph in :mod:`agent.graph.graph` is a single linear flow
``START -> retrieve -> llm -> END``. Each node takes an
``AgentState`` and returns a partial dict that LangGraph merges
into the running state.

Keys
----
tenant_id:
    Opaque tenant ULID. Threaded through every node for log /
    RAG / LLM scoping. The graph state itself is per-turn, so
    cross-tenant leakage is structurally impossible.
conversation_id:
    Opaque conversation ULID. Used by RAG for log breadcrumbs
    only — RAG scoping is tenant-scoped, not conversation-scoped.
messages:
    The conversation history mapped to LangChain ``BaseMessage``
    instances. Built by :class:`agent.simple_responder.SimpleResponder`
    before the graph runs. Synthetic system messages from
    history summarization are included here.
rag_messages:
    Synthetic ``SystemMessage`` block(s) produced by the retrieve
    node. Empty list means RAG yielded nothing (no KB / no hits /
    retrieval error) and the LLM gets no retrieval context.
final_text:
    The final assistant text produced by the LLM node. ``None``
    until ``llm_node`` runs; the caller (``respond()``) wraps it
    into an ``AgentResponse``.

Why ``TypedDict`` (not Pydantic)?
---------------------------------
LangGraph reads/writes state via dict-merge semantics; Pydantic
models add validation overhead we don't need for an M1 turn
(one-shot graph invocation, no checkpoint persistence). A
TypedDict is the idiomatic choice and keeps the surface small.
"""
from __future__ import annotations

from typing import TypedDict

from langchain_core.messages import BaseMessage


class AgentState(TypedDict):
    """Per-turn state carried through the agent graph.

    All keys are required — LangGraph fills any missing key with
    ``None`` (or the TypedDict default), and the nodes depend on
    the keys being present rather than re-checking.
    """

    tenant_id: str
    conversation_id: str
    messages: list[BaseMessage]
    rag_messages: list[BaseMessage]
    final_text: str | None


__all__ = ["AgentState"]
