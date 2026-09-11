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

* **Tool call failure is non-fatal.** Stage 7.2's
  ``make_llm_node`` may invoke LangChain tools when the LLM
  decides to escalate. If the tool raises, the node logs a
  WARNING and falls back to ``escalated=False`` — the
  customer always gets *some* answer, even when escalation
  itself blows up.

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
from langchain_core.tools import BaseTool

from agent.graph.prompts import (
    CHAT_MAX_TOKENS,
    CHAT_TEMPERATURE,
    ESCALATION_TOOL_NAME,
    FALLBACK_MESSAGE,
    M1_SYSTEM_PROMPT,
)
from agent.graph.state import AgentState
from agent.graph.tools import make_escalate_tool
from conversation.service import ConversationService
from core.logging import get_logger
from knowledge.rag_service import RAGService
from knowledge.retriever import KnowledgeBaseNotFoundError
from llm_client.client import LLMClient
from llm_client.exceptions import (
    InvalidRequest,
    OutputInvalid,
    ProviderUnavailable,
    RateLimited,
)
from llm_client.types import (
    ChatMessage as LLMChatMessage,
)
from llm_client.types import (
    ChatRequest,
)
from llm_client.types import (
    MessageRole as LLMMessageRole,
)
from llm_client.types import EmbeddingError

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
        # Defensive guard — Stage 7.1 review nit. A caller that
        # forgets to populate ``state["messages"]`` (or passes
        # ``None`` explicitly) should not crash the node with
        # ``TypeError``; the LLM gets no RAG context and the
        # turn proceeds with whatever the LLM already knows.
        if state.get("messages") is None:
            log.warning(
                "agent.graph.retrieve_messages_none",
                tenant_id=state["tenant_id"],
                conversation_id=state["conversation_id"],
            )
            return {"rag_messages": []}

        query = _latest_customer_text(state["messages"])
        if query is None:
            return {"rag_messages": []}

        try:
            rag_context = await rag_service.build_context_for_query(
                tenant_id=state["tenant_id"],
                conversation_id=state["conversation_id"],
                query=query,
            )
        except (EmbeddingError, KnowledgeBaseNotFoundError) as exc:
            # Typed catch — these are the structural RAG failure
            # modes the retriever explicitly raises. Logged as
            # WARNING so an operator can grep for them; the
            # customer never sees a 500 because of a retrieval
            # glitch. PII-safe payload — no chunk text, no query.
            log.warning(
                "agent.graph.retrieve_failed",
                tenant_id=state["tenant_id"],
                conversation_id=state["conversation_id"],
                error_type=type(exc).__name__,
            )
            return {"rag_messages": []}
        except Exception as exc:
            # Defence-in-depth safety net — should NEVER fire
            # because RAGService already swallows its own
            # exceptions, but if a regression sneaks in we
            # refuse to crash the AI auto-reply. Empty
            # rag_messages + WARNING + error_type. PII-safe:
            # no exc_info, no repr, no customer text.
            log.warning(
                "agent.graph.retrieve_failed_unexpected",
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
    tools: list[BaseTool] | None = None,
    conv_service: ConversationService | None = None,
) -> Callable[[AgentState], Coroutine[Any, Any, dict[str, Any]]]:
    """Build a configured LLM node bound to ``llm_client_factory`` and ``model``.

    The returned coroutine:
      1. Assembles the message list:
         ``[SystemMessage(M1_SYSTEM_PROMPT), *rag_messages, *messages]``.
      2. Calls ``LLMClient.chat()`` — with ``tools=[...]`` schema
         dicts if ``tools`` was provided to the factory or built
         from ``conv_service`` + state.
      3. Dispatches any ``escalate_to_human`` tool call back to the
         matching LangChain tool. On success sets
         ``state["escalated"] = True`` and stores the customer-facing
         message in ``state["escalation_message"]``.
      4. Returns ``{"final_text": response.content}`` on success or
         ``{"final_text": FALLBACK_MESSAGE}`` on any exception /
         empty content (fail-safe).

    This node is the last node in the graph and **must never
    raise** — a raise here would crash the graph and leave the
    customer without a response.

    Tool-call failure semantics
    ---------------------------

    If ``tool.ainvoke(...)`` raises (network blip, transient DB
    error, etc.) the node logs a WARNING with ``error_type`` and
    falls back to ``escalated=False`` so the LLM's normal text
    response (or :data:`FALLBACK_MESSAGE`) is preserved. A failed
    escalation MUST NOT take down the customer turn.

    Parameters
    ----------
    llm_client_factory:
        Per-tenant ``LLMClient`` factory.
    model:
        Model identifier forwarded to ``LLMClient.chat``.
    tools:
        Optional pre-built LangChain ``BaseTool`` list. When
        provided, these tools are advertised to the LLM and
        used for dispatch. Used by unit tests that want to spy
        on tool calls without wiring a real ``conv_service``.
    conv_service:
        Optional conversation service. When ``tools`` is not
        provided and ``conv_service`` is set, the LLM node
        builds the Stage 7.2 ``escalate_to_human`` tool PER
        TURN using the active state's ``tenant_id`` +
        ``conversation_id``. This is the production wiring.

        Stage 7.4 update — the LLM node uses
        :func:`agent.graph.tools.make_escalate_tool` (which
        reads tenant / conversation IDs from the module-level
        ``ContextVar`` set by ``SimpleResponder.respond``).
        When ``conv_service`` is provided and ``tools`` is not,
        the node constructs a fresh tool per turn; the
        ContextVar plumbing keeps tenant isolation intact and
        per-turn rebuild cost is dominated by the LLM round-trip
        (~500-2000ms vs <1ms for the tool build). Tests that
        want to spy on tool calls pass a pre-built ``tools``
        list and bypass the ``ContextVar`` entirely.
    """

    async def _node(state: AgentState) -> dict[str, Any]:
        tenant_id = state["tenant_id"]

        # Defensive guard — mirrors the retrieve_node guard.
        # LangGraph fills missing keys with None; an absent
        # ``messages`` list would crash ``_to_llm_chat_message``
        # below, so we short-circuit to the fallback before
        # that happens.
        if state.get("messages") is None:
            log.warning(
                "agent.graph.llm_messages_none",
                tenant_id=tenant_id,
                conversation_id=state["conversation_id"],
            )
            return {"final_text": FALLBACK_MESSAGE}

        # Resolve the per-turn toolset. Test wiring passes a
        # pre-built ``tools`` list; production lets the node
        # construct the escalation tool per turn (cheap; see
        # docstring).
        active_tools: list[BaseTool] = list(tools) if tools else []
        if not active_tools and conv_service is not None:
            active_tools = [
                make_escalate_tool(conv_service=conv_service)
            ]
        elif not active_tools:
            # No tools available: this happens when neither
            # ``tools`` nor ``conv_service`` were wired. M1 has
            # a default conv_service via SimpleResponder so this
            # branch should only fire in unit tests that
            # explicitly opt out.
            active_tools = []

        tool_schemas = (
            [_tool_to_anthropic_schema(t) for t in active_tools]
            if active_tools
            else []
        )
        tool_by_name = {t.name: t for t in active_tools}

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
                temperature=CHAT_TEMPERATURE,
                max_tokens=CHAT_MAX_TOKENS,
                tools=tool_schemas or None,
            )
            response = await client.chat(request)
        except (
            RateLimited,
            ProviderUnavailable,
            OutputInvalid,
            InvalidRequest,
        ) as exc:
            # Typed catch — these are the LLM client's documented
            # error modes. ``RateLimited`` and ``ProviderUnavailable``
            # are transient; ``OutputInvalid`` is a parse failure;
            # ``InvalidRequest`` is a programmer error. None of
            # them should ever take down the customer turn.
            log.warning(
                "agent.graph.llm_failed",
                tenant_id=tenant_id,
                conversation_id=state["conversation_id"],
                error_type=type(exc).__name__,
            )
            return {"final_text": FALLBACK_MESSAGE}
        except Exception as exc:
            # Defence-in-depth safety net — should NEVER fire
            # because the typed set above covers every documented
            # LLM-client error mode, but if a regression sneaks
            # in we refuse to crash the AI auto-reply. PII-safe:
            # no exc_info, no repr, no customer text.
            log.warning(
                "agent.graph.llm_failed_unexpected",
                tenant_id=tenant_id,
                conversation_id=state["conversation_id"],
                error_type=type(exc).__name__,
            )
            return {"final_text": FALLBACK_MESSAGE}

        # ---- Tool call dispatch (Stage 7.2) -------------------------
        # The LLM may have decided to escalate. We only dispatch
        # tools we recognise — anything else is logged and
        # ignored so the customer's turn is never blocked by a
        # hallucinated tool name. ``getattr`` with a default keeps
        # 7.1's plain-text fakes (``_StubChatResponse`` without
        # ``tool_calls``) working without modification.
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            # No tool calls — fall through to the normal text path.
            pass
        else:
            # Single-tool-call dispatch (Stage 7.4 hardening).
            # M1's tool surface has exactly one entry point
            # (``escalate_to_human``); dispatching more than one
            # would invite inconsistent state transitions. We
            # honour the FIRST tool call and log a WARNING for
            # any extras so an operator can detect a misbehaving
            # provider without taking the customer's turn down.
            first = tool_calls[0]
            for extra in tool_calls[1:]:
                extra_name = (
                    extra.get("name") if isinstance(extra, dict) else "<unparsed>"
                )
                log.warning(
                    "agent.graph.extra_tool_calls_ignored",
                    tenant_id=tenant_id,
                    conversation_id=state["conversation_id"],
                    tool_name=str(extra_name or "<missing>"),
                )

            if not isinstance(first, dict):
                log.warning(
                    "agent.graph.tool_call_unparseable",
                    tenant_id=tenant_id,
                    conversation_id=state["conversation_id"],
                    error_type=type(first).__name__,
                )
            else:
                name = first.get("name")
                if name != ESCALATION_TOOL_NAME:
                    log.info(
                        "agent.graph.unknown_tool_call",
                        tenant_id=tenant_id,
                        conversation_id=state["conversation_id"],
                        tool_name=name or "<missing>",
                    )
                else:
                    tool_obj = tool_by_name.get(name)
                    if tool_obj is None:
                        # Should not happen — factory built
                        # tools only for names it knows.
                        # Defensive log + fallback.
                        log.warning(
                            "agent.graph.tool_not_registered",
                            tenant_id=tenant_id,
                            conversation_id=state["conversation_id"],
                            tool_name=name,
                        )
                        return _fallback_to_text(response, tenant_id, state)

                    # The Anthropic provider returns raw
                    # ``tool_use`` blocks with the args under
                    # ``input``. OpenAI-style payloads nest them
                    # under ``args`` / ``function.arguments``.
                    # We accept either to keep the dispatcher
                    # provider-agnostic.
                    args = _extract_tool_args(first)
                    try:
                        tool_result = await tool_obj.ainvoke(args)
                    except Exception as exc:
                        # Tool failure MUST NOT take down the
                        # turn. Fall back to whatever the LLM
                        # originally returned.
                        log.warning(
                            "agent.graph.tool_call_failed",
                            tenant_id=tenant_id,
                            conversation_id=state["conversation_id"],
                            tool_name=name,
                            error_type=type(exc).__name__,
                        )
                        return _fallback_to_text(response, tenant_id, state)

                    # The tool returned
                    # ``{"escalated": True, "reason": "..."}``.
                    # The customer-facing message is the
                    # ``reason`` text (the same text the LLM
                    # would have surfaced anyway).
                    message = _tool_result_to_message(tool_result)
                    if message is None:
                        log.warning(
                            "agent.graph.tool_result_unparseable",
                            tenant_id=tenant_id,
                            conversation_id=state["conversation_id"],
                            tool_name=name,
                        )
                        return _fallback_to_text(response, tenant_id, state)

                    return {
                        "escalated": True,
                        "escalation_message": message,
                    }

        # ---- Normal text path ---------------------------------------
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


def make_escalation_node() -> Callable[
    [AgentState], Coroutine[Any, Any, dict[str, Any]]
]:
    """Build the trivial escalation terminal node.

    Reads ``state["escalation_message"]`` (written by
    :func:`make_llm_node` when the LLM fired the
    ``escalate_to_human`` tool) and writes it into
    ``state["final_text"]``. The node is intentionally tiny —
    its only purpose is to make the post-escalation step
    auditable in the graph topology. The conversation-side
    effects (status flip, ``ai_handling=False``) were already
    applied by the tool's ``escalate_to_human_queue`` call.
    """

    async def _node(state: AgentState) -> dict[str, Any]:
        message = state.get("escalation_message")
        if not isinstance(message, str) or not message.strip():
            # Defensive: if the upstream node failed to populate
            # ``escalation_message``, degrade to the generic
            # fallback so the customer is never left without a
            # response. The conversation has already been
            # flipped out of AI handling by the tool.
            log.warning(
                "agent.graph.escalation_message_missing",
                tenant_id=state["tenant_id"],
                conversation_id=state["conversation_id"],
            )
            return {"final_text": FALLBACK_MESSAGE}
        return {"final_text": message}

    return _node


def _fallback_to_text(
    response: Any,
    tenant_id: str,
    state: AgentState,
) -> dict[str, Any]:
    """Reduce a successful LLM response to its text portion when
    tool dispatch fails.

    Returns ``{"final_text": <response.content or FALLBACK_MESSAGE>}``
    — we deliberately do NOT include ``escalated: False`` so the
    state default propagates. Never raises.
    """
    text = getattr(response, "content", None) or ""
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        log.warning(
            "agent.graph.tool_fallback_empty",
            tenant_id=tenant_id,
            conversation_id=state["conversation_id"],
        )
        return {"final_text": FALLBACK_MESSAGE}
    return {"final_text": text}


def _extract_tool_args(call: dict[str, Any]) -> dict[str, Any]:
    """Extract the args dict from a provider-agnostic ``tool_call`` entry.

    Handles the three payload shapes we have seen across
    providers:

    * Anthropic ``tool_use``: ``{"input": {...}}``
    * OpenAI function-call: ``{"function": {"arguments": str | dict}}``
    * Generic: ``{"args": {...}}``

    Returns ``{}`` when no recognised shape is found — LangChain's
    tool will then complain about a missing required field and
    surface the error through the standard tool-ainvoke failure
    path, which the node logs + falls back from.
    """
    if "input" in call and isinstance(call["input"], dict):
        return call["input"]
    if "args" in call and isinstance(call["args"], dict):
        return call["args"]
    fn = call.get("function")
    if isinstance(fn, dict):
        arguments = fn.get("arguments")
        if isinstance(arguments, dict):
            return arguments
        if isinstance(arguments, str):
            import json

            try:
                parsed = json.loads(arguments)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return {}
    return {}


def _tool_result_to_message(result: Any) -> str | None:
    """Convert a LangChain tool's ainvoke result into the customer-facing
    escalation message string.

    The ``escalate_to_human`` tool returns
    ``{"escalated": True, "reason": "..."}``. We pull out
    ``reason`` (the customer-facing text) and return it. For any
    other shape (str, ToolMessage, None) we apply a best-effort
    coercion so the test suite's plain-dict spy contract still
    works.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        reason = result.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason
        # ``ToolMessage`` content may also be a string; some
        # tool wrappers wrap the result in a content attribute.
        content = result.get("content")
        if isinstance(content, str) and content.strip():
            return content
    content_attr = getattr(result, "content", None)
    if isinstance(content_attr, str) and content_attr.strip():
        return content_attr
    return None


def _tool_to_anthropic_schema(tool: BaseTool) -> dict[str, Any]:
    """Build an Anthropic-compatible ``tools`` payload entry from a
    LangChain ``BaseTool``.

    Anthropic expects::

        {
            "name": "...",
            "description": "...",
            "input_schema": {"type": "object", "properties": {...}}
        }

    The LangChain tool's ``.args`` exposes the ``properties``
    half of the JSON Schema only; we wrap it into a full
    ``input_schema`` here. Required-field extraction is omitted
    intentionally — the LangChain tool's own validator catches
    missing args at dispatch time, and Anthropic accepts
    properties without ``required`` as "all optional".
    """
    # ``getattr`` keeps this resilient to duck-typed fakes in
    # tests (some test tools implement only ``ainvoke`` + ``name``
    # and skip the JSON-Schema args surface).
    properties = getattr(tool, "args", None)
    if not isinstance(properties, dict):
        properties = {}
    description = getattr(tool, "description", "") or ""
    return {
        "name": tool.name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
        },
    }


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
    "make_escalation_node",
    "make_llm_node",
    "make_retrieve_node",
    "retrieve_node",
]
