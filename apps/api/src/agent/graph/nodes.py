"""LangGraph node implementations for the M1 agent graph.

Design contract
---------------

* Each node is a small ``async`` function that takes an
  :class:`agent.graph.state.AgentState` and returns a dict that
  LangGraph merges into the running state.

* **No node ever raises.** ``retrieve_node`` and ``llm_node`` both
  swallow every exception, downgrade to a safe fallback, and
  log a WARNING with the exception class name only (no
  repr, no customer text). This is the same fail-open invariant
  the pre-LangGraph :class:`agent.simple_responder.SimpleResponder`
  upheld — RAG is an enhancement, not a hard dependency, and
  the customer must always get *some* answer (or a clearly
  labelled fallback).

* **Tenant isolation.** Every RAG / LLM call carries
  ``tenant_id`` from the graph state. ``conversation_id`` is
  threaded only as a log breadcrumb for RAG.

* **Stateless functions.** The factory functions
  :func:`make_retrieve_node` / :func:`make_llm_node` bind the
  per-tenant RAG service / LLM client factory once at graph
  construction time; the returned coroutine is the actual
  LangGraph node.

Why factories (not LangGraph runtime injection)?
-----------------------------------------------

M1 has one agent process per request and never re-enters the
graph; the per-tenant services do not change inside a turn.
Closing them over the factory keeps the node signatures
minimal and avoids dragging ``langgraph.runtime.Runtime`` /
``InjectedState`` annotations into call sites. Stage 7.2+ may
swap to runtime injection if multi-tenant concurrent graphs
appear.
"""
from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage

from agent.graph.prompts import FALLBACK_MESSAGE, M1_SYSTEM_PROMPT
from agent.graph.state import AgentState
from core.logging import get_logger
from knowledge.rag_service import RAGService
from llm_client.client import LLMClient
from llm_client.types import (
    ChatMessage as LLMChatMessage,
)
from llm_client.types import (
    ChatRequest,
)
from llm_client.types import (
    MessageRole as LLMMessageRole,
)

log = get_logger(__name__)

# Local type alias matching the per-tenant LLMClient factory used
# elsewhere in the agent package. We re-declare it here to avoid a
# circular import with ``agent.simple_responder``.
LLMClientFactory = Callable[[str], LLMClient]


def _latest_customer_text(messages: list[BaseMessage]) -> str | None:
    """Return the text of the most recent customer ``HumanMessage`` in
    ``messages``, or ``None`` when no customer turn exists.

    Walks the list in reverse so we short-circuit on the freshest
    customer message. ``BaseMessage`` is duck-typed — any subclass
    with a ``content`` string attribute works (HumanMessage,
    AIMessage, SystemMessage, etc.). We discriminate by type
    rather than role string to keep this node agnostic to
    LangChain's role encoding.
    """
    from langchain_core.messages import HumanMessage  # local import: typing cycle

    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            content = msg.content
            if isinstance(content, str) and content.strip():
                return content
    return None


async def retrieve_node(state: AgentState) -> dict[str, Any]:
    """Default retrieve node — expects ``_rag_service`` to be in scope.

    NOTE: this is the bare-bones node signature for tests; the
    production graph wires :func:`make_retrieve_node` instead. We
    raise ``RuntimeError`` here so accidental use in production
    fails loudly.
    """
    raise RuntimeError(
        "retrieve_node() was called without a wired RAG service. "
        "Use make_retrieve_node(...) to construct a configured node."
    )


async def llm_node(state: AgentState) -> dict[str, Any]:
    """Default LLM node — expects ``_llm_client_factory`` to be in scope.

    Same caveat as :func:`retrieve_node`: production graphs must
    use :func:`make_llm_node`.
    """
    raise RuntimeError(
        "llm_node() was called without a wired LLM client factory. "
        "Use make_llm_node(...) to construct a configured node."
    )


def make_retrieve_node(
    *,
    rag_service: RAGService,
) -> Callable[[AgentState], Coroutine[Any, Any, dict[str, Any]]]:
    """Build a configured retrieve node bound to ``rag_service``.

    The returned coroutine:
      1. Looks at ``state["messages"]`` for the latest customer turn.
      2. Calls :meth:`RAGService.build_context_for_query` for it.
      3. Returns ``{"rag_messages": [SystemMessage(...)]}`` on a hit,
         or ``{"rag_messages": []}`` when there is no customer turn /
         no hits / any exception (fail-open).

    Failures (KB lookup error, embedding error, retrieval error, etc.)
    are caught here as a defence-in-depth layer on top of the
    RAG service's own exception handling. The customer never sees
    a 500 because of a retrieval glitch.
    """

    async def _node(state: AgentState) -> dict[str, Any]:
        query = _latest_customer_text(state["messages"])
        if query is None:
            return {"rag_messages": []}

        try:
            rag_context = await rag_service.build_context_for_query(
                tenant_id=state["tenant_id"],
                conversation_id=state["conversation_id"],
                query=query,
            )
        except Exception as exc:
            # Defence-in-depth: RAGService already swallows its own
            # exceptions, but a regression must never take down the
            # AI auto-reply. Empty rag_messages + WARNING + error_type.
            log.warning(
                "agent.graph.retrieve_failed",
                tenant_id=state["tenant_id"],
                conversation_id=state["conversation_id"],
                error_type=type(exc).__name__,
            )
            return {"rag_messages": []}

        if rag_context.chunk_count == 0 or not rag_context.system_message:
            return {"rag_messages": []}

        return {
            "rag_messages": [SystemMessage(content=rag_context.system_message)],
        }

    return _node


def make_llm_node(
    *,
    llm_client_factory: LLMClientFactory,
    model: str,
) -> Callable[[AgentState], Coroutine[Any, Any, dict[str, Any]]]:
    """Build a configured LLM node bound to ``llm_client_factory`` and ``model``.

    The returned coroutine:
      1. Assembles the message list:
         ``[SystemMessage(M1_SYSTEM_PROMPT), *rag_messages, *messages]``.
      2. Calls ``LLMClient.chat()``.
      3. Returns ``{"final_text": response.content}`` on success or
         ``{"final_text": FALLBACK_MESSAGE}`` on any exception /
         empty content (fail-safe).

    This node is the last node in the graph and **must never
    raise** — a raise here would crash the graph and leave the
    customer without a response.
    """

    async def _node(state: AgentState) -> dict[str, Any]:
        tenant_id = state["tenant_id"]
        try:
            client = llm_client_factory(tenant_id)
            request = ChatRequest(
                model=model,
                messages=[
                    LLMChatMessage(role=LLMMessageRole.SYSTEM, content=M1_SYSTEM_PROMPT),
                    *[
                        _to_llm_chat_message(m)
                        for m in (
                            *state["rag_messages"],
                            *state["messages"],
                        )
                    ],
                ],
            )
            response = await client.chat(request)
        except Exception as exc:
            log.warning(
                "agent.graph.llm_failed",
                tenant_id=tenant_id,
                conversation_id=state["conversation_id"],
                error_type=type(exc).__name__,
            )
            return {"final_text": FALLBACK_MESSAGE}

        text = response.content.strip()
        if not text:
            log.warning(
                "agent.graph.llm_empty",
                tenant_id=tenant_id,
                conversation_id=state["conversation_id"],
            )
            return {"final_text": FALLBACK_MESSAGE}

        return {"final_text": response.content}

    return _node


def _to_llm_chat_message(msg: BaseMessage) -> LLMChatMessage:
    """Translate a LangChain ``BaseMessage`` to the existing LLM client's
    ``ChatMessage`` type.

    The :class:`LLMClient` (Stage 4) still speaks ``ChatMessage`` /
    ``MessageRole``. Stage 7.x keeps using it directly — we don't
    migrate to LangChain's chat-model wrappers yet (those land
    with tools in 7.2+). This adapter keeps the translation in
    one place.

    ``content`` may be either a ``str`` or a list of content
    blocks (LangChain 0.3+ multimodal). The LLM client only
    accepts ``str`` for M1; we collapse the block list to its
    textual representation so the existing client keeps working.
    """
    content = msg.content
    if isinstance(content, list):
        # ``content`` may be ``list[str | dict[str, Any]]`` (mixed
        # text + image blocks). Collapse to plain text for the M1
        # LLM client. Multimodal inputs are out of scope for 7.1.
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                text_parts.append(block)
            elif isinstance(block, dict) and "text" in block:
                text_value = block["text"]
                if isinstance(text_value, str):
                    text_parts.append(text_value)
        content = "\n".join(text_parts)
    if not isinstance(content, str):  # pragma: no cover - defensive
        content = str(content)

    role = _to_llm_role(msg)
    return LLMChatMessage(role=role, content=content)


def _to_llm_role(msg: BaseMessage) -> LLMMessageRole:
    """Map a LangChain message type to the LLM client's role enum."""
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
    )

    if isinstance(msg, HumanMessage):
        return LLMMessageRole.USER
    if isinstance(msg, AIMessage):
        return LLMMessageRole.ASSISTANT
    if isinstance(msg, SystemMessage):
        return LLMMessageRole.SYSTEM
    # ToolMessage / others — fall back to user. M1 doesn't drive tools
    # from the graph; this branch only triggers for legacy TOOL rows
    # the caller forgot to strip.
    return LLMMessageRole.USER


__all__ = [
    "llm_node",
    "make_llm_node",
    "make_retrieve_node",
    "retrieve_node",
]
