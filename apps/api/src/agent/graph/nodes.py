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
    ChatResponse,
    EmbeddingError,
)
from llm_client.types import (
    MessageRole as LLMMessageRole,
)

log = get_logger(__name__)

# Local type alias matching the per-tenant LLMClient factory used
# elsewhere in the agent package. We re-declare it here to avoid a
# circular import with ``agent.simple_responder``.
LLMClientFactory = Callable[[str], LLMClient]

# Stage 12 / Task 2 — maximum number of tool-call dispatches the
# LLM node will perform in a single turn before bailing out with
# :data:`FALLBACK_MESSAGE`. M1 used a first-wins shortcut
# (``tool_calls[:1]``) which made multi-step tool flows
# impossible; the new loop dispatches every tool call the LLM
# emits. A misbehaving LLM (or a runaway tool) must NOT be able
# to keep the customer's turn alive forever, so the loop bails
# after this many iterations and falls back to the safe message.
_MAX_TOOL_ITERATIONS = 5


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
      2. Calls ``LLMClient.chat()`` (or ``stream_chat`` when the
         caller passed an ``on_delta`` callback) — with
         ``tools=[...]`` schema dicts if ``tools`` was provided to
         the factory or built from ``conv_service`` + state.
      3. Dispatches every tool call the LLM emits in a loop (Stage 12
         Task 2 — pre-Task-2 the node used a ``tool_calls[:1]``
         first-wins shortcut that dispatched only the first tool
         call and dropped the rest). For each successful dispatch
         the node appends a ``ToolMessage``-equivalent
         ``ChatMessage(role=TOOL, content=...)`` row to the running
         message list, then re-invokes the LLM via ``client.chat``
         (loop iterations always use the non-streaming surface —
         the customer's first-text UX is driven by the *initial*
         stream; subsequent re-invocations are batched). The loop
         bails out via :data:`_MAX_TOOL_ITERATIONS` = 5 so a
         misbehaving provider cannot keep the customer turn alive
         forever.
      4. Escalation short-circuit — when a successful
         ``escalate_to_human`` dispatch lands mid-batch the node
         returns ``{"escalated": True, "escalation_message": ...}``
         immediately (no LLM re-invocation) so the graph's
         conditional edge routes to the escalation terminal.
         Sibling tool_calls in the same response are logged as
         ``extra_tool_calls_ignored`` and not dispatched.
      5. Returns ``{"final_text": response.content}`` on the normal
         text path or ``{"final_text": FALLBACK_MESSAGE}`` on any
         exception / empty content (fail-safe).

    Every return path also includes ``tool_iterations`` (read from
    ``state["tool_iterations"]`` on entry, incremented on each
    loop dispatch) so downstream metrics can observe the iteration
    count without parsing log lines.

    This node is the last node in the graph and **must never
    raise** — a raise here would crash the graph and leave the
    customer without a response.

    Tool-call failure semantics
    ---------------------------

    If ``tool.ainvoke(...)`` raises (network blip, transient DB
    error, unknown tool name, etc.) the node logs a WARNING with
    ``error_type`` and writes a structured
    ``"Error: tool '<name>' failed (<error_type>)"`` (or
    ``"Error: unknown tool '<name>'"``) string into the
    ToolMessage content. The loop then CONTINUES — the LLM sees
    the error on the next invocation and can retry, fall back to
    a text answer, or call a different tool. A failed dispatch
    NEVER aborts the customer turn.

    The escalation tool is the one exception: ``escalate_to_human``
    is a *terminal* tool. Once it succeeds the conversation has
    already been flipped at the DB level by its side effect, so
    calling the LLM again would only risk the model overriding
    the escalation. We short-circuit on successful
    ``escalate_to_human`` dispatch (see step 4 above).

    Streaming-vs-non-streaming split
    --------------------------------

    The FIRST LLM call uses ``stream_chat`` when ``state`` carries
    an ``on_delta`` callback so the customer sees incremental text
    deltas over the WS layer. ALL subsequent re-invocations inside
    the loop use ``client.chat`` (non-streaming) for determinism —
    the streaming path's text-delta contract is only meaningful
    for the initial response. A WS hiccup during the initial
    stream is logged + ignored so the customer turn still
    completes.

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

        # Stage 12 / Task 2 — seed the iteration counter from the
        # state default so the value is observable for metrics /
        # debugging. The loop increments this on each successful
        # dispatch; every return path below includes
        # ``tool_iterations`` so downstream consumers can read it.
        iterations = int(state.get("tool_iterations") or 0)

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
            return {
                "final_text": FALLBACK_MESSAGE,
                "tool_iterations": iterations,
            }

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
            # Mutable message list — Stage 12 / Task 2 tool loop
            # appends a ``ToolMessage`` (ChatMessage(role=TOOL, ...))
            # for each dispatch and rebuilds the ``ChatRequest`` from
            # the running list. Keeping the list hoisted (rather
            # than rebuilding from ``state`` on every iteration)
            # matches the documented LangGraph tool-loop pattern.
            llm_messages: list[LLMChatMessage] = [
                LLMChatMessage(role=LLMMessageRole.SYSTEM, content=M1_SYSTEM_PROMPT),
                *[
                    _to_llm_chat_message(m)
                    for m in (
                        *state["rag_messages"],
                        *state["messages"],
                    )
                ],
            ]

            def _build_request() -> ChatRequest:
                return ChatRequest(
                    model=model,
                    messages=llm_messages,
                    temperature=CHAT_TEMPERATURE,
                    max_tokens=CHAT_MAX_TOKENS,
                    tools=tool_schemas or None,
                )

            request = _build_request()
            on_delta = state.get("on_delta")
            if on_delta is not None:
                streamed: ChatResponse | None = None
                async for item in client.stream_chat(request):
                    if isinstance(item, ChatResponse):
                        streamed = item
                        continue
                    # item is a plain text delta; relay it to the WS layer.
                    # A WS hiccup must NEVER take down the customer turn, so
                    # failures here are logged and the stream continues.
                    try:
                        await on_delta(str(item))
                    except Exception:
                        log.warning(
                            "agent.graph.stream_delta_failed",
                            tenant_id=tenant_id,
                            conversation_id=state["conversation_id"],
                            error_type=type(item).__name__,
                        )
                if streamed is None:
                    raise ProviderUnavailable("stream ended without a final response")
                response = streamed
            else:
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
            return {
                "final_text": FALLBACK_MESSAGE,
                "tool_iterations": iterations,
            }
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
            return {
                "final_text": FALLBACK_MESSAGE,
                "tool_iterations": iterations,
            }

        # ---- Tool call loop (Stage 12 / Task 2) -------------------
        # The LLM may decide to invoke zero or more tools before
        # producing its final answer. Stage 7.2 / 7.4 used a
        # first-wins shortcut (``tool_calls[:1]``) that could only
        # dispatch the first tool call and silently dropped the
        # rest. The loop below dispatches EVERY tool call the LLM
        # emits, feeds the results back into the message list as
        # ``ToolMessage``-equivalent ``ChatMessage(role=TOOL, ...)``
        # rows, and re-invokes the LLM. The loop bails out after
        # :data:`_MAX_TOOL_ITERATIONS` successful dispatches so a
        # misbehaving provider cannot keep the customer's turn
        # alive forever.
        #
        # Escalation semantics (M1 contract preserved)
        # ---------------------------------------------
        # The ``escalate_to_human`` tool is a *terminal* tool: once
        # it succeeds the conversation is already flipped at the DB
        # level via its side effect (``escalate_to_human_queue``),
        # so calling the LLM again would only risk the model
        # overriding the escalation with new tool calls. We
        # therefore short-circuit on a successful
        # ``escalate_to_human`` dispatch — surface
        # ``escalated=True`` + ``escalation_message`` so the
        # graph's conditional edge routes to the escalation
        # terminal, and log a WARNING for any sibling tool calls
        # the LLM bundled in the same response (these are
        # "extras" in the M1 sense). For all OTHER tools the loop
        # continues normally — the M2 contract lets the LLM see
        # the tool result and produce a final answer.
        #
        # Errors that surface inside the dispatch path are caught
        # locally so a flaky tool never aborts the whole turn — we
        # write the error string into the ToolMessage and continue.
        # PII-safe logging throughout: opaque IDs, error_type
        # names, no message content, no tool args, no tool results.

        while True:
            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                break

            iterations += 1
            if iterations > _MAX_TOOL_ITERATIONS:
                # M1 contract — this node must never raise and
                # must always return some answer to the customer.
                # A runaway tool loop falls back to the safe
                # fallback message; the conversation log records
                # ``iterations`` so an operator can spot the
                # misbehaving provider.
                log.warning(
                    "agent.graph.tool_loop_max_iterations_exceeded",
                    tenant_id=tenant_id,
                    conversation_id=state["conversation_id"],
                    iterations=iterations,
                )
                return {
                    "final_text": FALLBACK_MESSAGE,
                    "tool_iterations": iterations,
                }

            # Dispatch every tool call in the response. For each
            # dispatch we may either (a) append a ToolMessage and
            # continue the loop, (b) short-circuit with an
            # escalation result, or (c) bail out via max_iterations
            # (handled above).
            for idx, tc in enumerate(tool_calls):
                if not isinstance(tc, dict):
                    log.warning(
                        "agent.graph.tool_call_unparseable",
                        tenant_id=tenant_id,
                        conversation_id=state["conversation_id"],
                        error_type=type(tc).__name__,
                    )
                    tool_call_id: str | None = None
                    tool_name: str | None = None
                    tool_result_str = (
                        f"Error: unparseable tool call "
                        f"({type(tc).__name__})"
                    )
                    tool_succeeded = False
                else:
                    tool_call_id = tc.get("id")
                    tool_name = tc.get("name")
                    tool_obj = (
                        tool_by_name.get(tool_name) if tool_name else None
                    )
                    if tool_obj is None:
                        # The LLM hallucinated a tool name we
                        # didn't advertise (or the tool factory
                        # was wired with a different surface).
                        # Either way, the customer MUST still
                        # get an answer — we feed the LLM an
                        # error string as the tool result and
                        # continue the loop so it can recover.
                        log.warning(
                            "agent.graph.unknown_tool_call",
                            tenant_id=tenant_id,
                            conversation_id=state["conversation_id"],
                            tool_name=str(tool_name or "<missing>"),
                        )
                        tool_result_str = (
                            f"Error: unknown tool '{tool_name}'"
                        )
                        tool_succeeded = False
                    else:
                        # Anthropic nests tool args under
                        # ``input``; OpenAI nests them under
                        # ``args`` / ``function.arguments``. The
                        # helper handles both shapes so the
                        # dispatcher stays provider-agnostic.
                        args = _extract_tool_args(tc)
                        try:
                            tool_result = await tool_obj.ainvoke(args)
                        except Exception as exc:
                            # Tool failure MUST NOT abort the
                            # turn. Write the error into the
                            # ToolMessage and continue so the LLM
                            # can either retry or formulate a
                            # text response.
                            log.warning(
                                "agent.graph.tool_call_failed",
                                tenant_id=tenant_id,
                                conversation_id=state["conversation_id"],
                                tool_name=str(tool_name or "<missing>"),
                                error_type=type(exc).__name__,
                            )
                            tool_result_str = (
                                f"Error: tool '{tool_name}' failed "
                                f"({type(exc).__name__})"
                            )
                            tool_succeeded = False
                        else:
                            tool_succeeded = True
                            tool_result_str = _tool_result_to_string(
                                tool_result
                            )

                # ---- Escalation short-circuit (M1 contract) ----
                # When ``escalate_to_human`` succeeds, the
                # conversation state has already been flipped at
                # the DB level by the tool's side effect — calling
                # the LLM again would only risk the model
                # overriding the escalation. We log any sibling
                # tool calls (the "extras ignored" M1 pattern) and
                # return the escalation result so the graph routes
                # to the escalation terminal.
                if (
                    tool_succeeded
                    and tool_name == ESCALATION_TOOL_NAME
                ):
                    for extra in tool_calls[idx + 1 :]:
                        extra_name = (
                            extra.get("name")
                            if isinstance(extra, dict)
                            else "<unparsed>"
                        )
                        log.warning(
                            "agent.graph.extra_tool_calls_ignored",
                            tenant_id=tenant_id,
                            conversation_id=state["conversation_id"],
                            tool_name=str(extra_name or "<missing>"),
                        )
                    return {
                        "escalated": True,
                        "escalation_message": _tool_result_to_message(
                            tool_result
                        )
                        if tool_succeeded
                        else tool_result_str,
                        "tool_iterations": iterations,
                    }

                # Otherwise: append the ToolMessage-equivalent to
                # the running message list so the next LLM call
                # sees the tool result alongside the assistant's
                # prior turn.
                llm_messages.append(
                    LLMChatMessage(
                        role=LLMMessageRole.TOOL,
                        content=tool_result_str,
                        tool_call_id=tool_call_id,
                        name=tool_name,
                    )
                )

            # Re-invoke the LLM with the updated message list.
            # Loop iterations always use ``client.chat`` (not the
            # streaming surface) — the customer's first-text UX
            # is driven by the *initial* stream; subsequent
            # dispatches are batched to keep the loop simple and
            # deterministic.
            try:
                request = _build_request()
                response = await client.chat(request)
            except (
                RateLimited,
                ProviderUnavailable,
                OutputInvalid,
                InvalidRequest,
            ) as exc:
                log.warning(
                    "agent.graph.llm_failed",
                    tenant_id=tenant_id,
                    conversation_id=state["conversation_id"],
                    error_type=type(exc).__name__,
                )
                return {
                    "final_text": FALLBACK_MESSAGE,
                    "tool_iterations": iterations,
                }
            except Exception as exc:
                log.warning(
                    "agent.graph.llm_failed_unexpected",
                    tenant_id=tenant_id,
                    conversation_id=state["conversation_id"],
                    error_type=type(exc).__name__,
                )
                return {
                    "final_text": FALLBACK_MESSAGE,
                    "tool_iterations": iterations,
                }

        # ---- Loop exit (no more tool_calls) -----------------------
        text = response.content.strip() if isinstance(response.content, str) else ""
        if not text:
            log.warning(
                "agent.graph.llm_empty",
                tenant_id=tenant_id,
                conversation_id=state["conversation_id"],
            )
            return {
                "final_text": FALLBACK_MESSAGE,
                "tool_iterations": iterations,
            }

        return {
            "final_text": response.content,
            "tool_iterations": iterations,
        }

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


def _tool_result_to_string(result: Any) -> str:
    """Convert a LangChain tool's ainvoke result into the string
    that flows into the follow-up ``ToolMessage`` content.

    Stage 12 / Task 2 — the tool loop feeds tool results back to
    the LLM via ``ChatMessage(role=TOOL, content=...)``. Strings
    pass through; dicts collapse to ``reason`` (the customer-facing
    text) or ``content`` (for ``ToolMessage`` wrappers); anything
    else falls back to ``str(result)`` so the LLM sees *some*
    signal rather than a TypeError.

    Errors are already pre-formatted as ``"Error: ..."`` strings
    by the caller (the dispatch loop), so this helper only sees
    successful tool results.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        # Mirrors ``_tool_result_to_message`` — prefer the
        # customer-facing ``reason`` first, then any ``content``
        # field. Falls back to the dict repr as a last resort.
        reason = result.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason
        content = result.get("content")
        if isinstance(content, str) and content.strip():
            return content
    content_attr = getattr(result, "content", None)
    if isinstance(content_attr, str) and content_attr.strip():
        return content_attr
    return str(result)


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
