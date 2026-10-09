"""Tool-call dispatch loop extracted from ``agent.graph.nodes``.

Stage 12 / Task 2 turned ``make_llm_node``'s first-wins shortcut
(``tool_calls[:1]``) into a proper loop that dispatches every tool
call the LLM emits, feeds the results back, and re-invokes the LLM
until the model stops emitting tool calls. The loop body was ~190
lines and lived inline in the closure — large enough to deserve its
own module so each branch (known tool / unknown tool / escalating
tool / max iterations / LLM error on re-call) can be reasoned about
in isolation.

Why the loop body's complexity lives here, not in ``nodes.py``
--------------------------------------------------------------

The 500-line ``make_llm_node`` closure was the obvious smell. Splitting
out the *pure* parts (``_invocation.assemble_llm_messages``) was Task 9's
first half; extracting the *imperative* tool-loop body is the second.
The caller still owns:

* LLM client creation (factory call)
* The first request/stream to the LLM (streaming vs non-streaming)
* The loop-exit final-return shape
* All escalation-path propagation

What this module owns:
* Per-iteration bookkeeping (count, max guard)
* Per-``tool_call`` dispatch (known / unknown / failed / escalation)
* ``ToolMessage`` row appending onto the running ``llm_messages`` list
* Re-invocation of the LLM with the updated list
* LLM-error fallback inside the loop

The return contract
-------------------

``dispatch_tool_calls`` returns a 3-tuple
``(result_dict_or_None, iterations_after, final_response)``:

* ``(None, iterations_after, final_response)`` — loop exhausted
  cleanly. ``final_response`` is the latest :class:`ChatResponse`
  (the input ``response`` when no re-invocation was needed; the
  most recent re-invocation otherwise). The caller reads
  ``final_response.content`` to build the final return.
* ``(result_dict, iterations_after, final_response)`` — a
  terminal path was reached (escalation short-circuit,
  max-iterations cap, or LLM error during re-invoke).
  ``result_dict`` matches the LangGraph partial-state shape
  ``make_llm_node`` returns on its terminal paths; the caller
  propagates it. ``final_response`` is still returned for
  symmetry but the caller never reads it on this branch.

Why ``None`` vs a sentinel for the result dict?
* A sentinel would carry a value the caller would compare against
  on every call. ``None`` is the Python idiom for "absent" and
  makes the call sites read like an early-return pattern.

Why does the function return the latest response?
* The original inline loop reassigned the closure-local
  ``response`` variable after every re-invocation. Extracting
  the loop into a function means the function-local
  ``response`` no longer leaks back to the caller; we close
  that gap by including the latest response in the return
  tuple. Without this, the caller would still hold the
  *first* ``response`` (typically a tool-call response with
  empty content) and ship that to the customer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool

from agent.graph.nodes import (  # downstream import kept here for clarity
    _extract_tool_args,
    _tool_result_to_message,
    _tool_result_to_string,
)
from agent.graph.prompts import ESCALATION_TOOL_NAME, FALLBACK_MESSAGE
from agent.graph.state import AgentState
from core.logging import get_logger
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
)
from llm_client.types import (
    MessageRole as LLMMessageRole,
)

log = get_logger(__name__)


# ``_build_request`` style helper threaded through. The closure inside
# ``nodes.py`` builds ``ChatRequest`` from the running ``llm_messages``
# + ``model``. Tests that want to exercise this module directly can
# inject their own builder; production wiring uses the closure.
RequestBuilder = Callable[[], ChatRequest]


async def dispatch_tool_calls(
    *,
    response: ChatResponse,
    tool_by_name: dict[str, BaseTool],
    llm_messages: list[LLMChatMessage],
    state: AgentState,
    client: LLMClient,
    build_request: RequestBuilder,
    iterations: int,
    max_iterations: int,
) -> tuple[dict[str, Any] | None, int, ChatResponse]:
    """Run the tool-call loop until the LLM emits no further tool calls.

    Parameters
    ----------
    response:
        The latest :class:`ChatResponse` — typically the response from
        the LLM's most recent invocation. The loop inspects
        ``response.tool_calls``; if empty, it returns
        ``(None, iterations, response)`` immediately so the caller can
        build the final-return dict from ``response.content``.
    tool_by_name:
        Resolved ``name -> BaseTool`` mapping. Tools not in this
        dict are treated as *unknown* (logged + LLM error row).
    llm_messages:
        Mutable running message list. ``ToolMessage``-equivalent
        rows are appended here on every successful (or failed)
        dispatch so the next LLM invocation sees the tool results.
        The leading ``system_prompt`` and RAG rows must already be
        in the list — this function only adds ``TOOL`` rows.
    state:
        The current :class:`AgentState`. Used here only for log
        breadcrumbs (``tenant_id``, ``conversation_id``); the
        function does not mutate state itself.
    client:
        The per-tenant :class:`LLMClient`. Used to re-invoke the
        LLM with the updated ``llm_messages`` after every iteration.
    build_request:
        Callable that returns a fresh :class:`ChatRequest` built
        from the current ``llm_messages`` + ``model``/``tools``.
        Splitting this out keeps ``dispatch_tool_calls``
        independent of the streaming-vs-non-streaming plumbing in
        ``make_llm_node``.
    iterations:
        Current iteration count *as observed by the caller*. The
        function increments this once per loop entry (after the
        ``tool_calls`` guard returns), so an N-iteration run
        returns ``iterations + N``.
    max_iterations:
        Hard cap. The function increments-then-checks; once the
        counter would exceed ``max_iterations``, the function
        returns the fallback message and bails out.

    Returns
    -------
    tuple[dict[str, Any] | None, int, ChatResponse]
        ``(result_dict_or_None, iterations_after, final_response)``.
        ``None`` for the first slot means the loop exhausted
        cleanly — the caller reads ``final_response.content`` to
        build the final return. A dict means a terminal path
        (escalation short-circuit, max-iterations cap, or LLM
        error during re-invoke) was reached and the caller
        should propagate the dict.
    """
    tenant_id = state["tenant_id"]
    conversation_id = state["conversation_id"]

    # Track the most recent ``response`` so we can return it on the
    # clean-exit branch. The loop body reassigns ``current_response``
    # after every re-invocation; on the clean-exit branch we return
    # the latest value the LLM produced.
    current_response = response

    while True:
        tool_calls = getattr(current_response, "tool_calls", None) or []
        if not tool_calls:
            break

        iterations += 1
        if iterations > max_iterations:
            # M1 contract — this node must never raise and
            # must always return some answer to the customer.
            # A runaway tool loop falls back to the safe
            # fallback message; the conversation log records
            # ``iterations`` so an operator can spot the
            # misbehaving provider.
            log.warning(
                "agent.graph.tool_loop_max_iterations_exceeded",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                iterations=iterations,
            )
            return (
                {
                    "final_text": FALLBACK_MESSAGE,
                    "escalated": False,
                    "tool_iterations": iterations,
                },
                iterations,
                current_response,
            )

        # Dispatch every tool call in the response. For each
        # dispatch we may either (a) append a ToolMessage and
        # continue the loop, (b) short-circuit with an
        # escalation result, or (c) bail out via max_iterations
        # (handled above).
        for idx, tc in enumerate(tool_calls):
            tool_result_str: str
            tool_succeeded: bool
            tool_result: Any
            if not isinstance(tc, dict):
                log.warning(
                    "agent.graph.tool_call_unparseable",
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    error_type=type(tc).__name__,
                )
                tool_call_id: str | None = None
                tool_name: str | None = None
                tool_result_str = f"Error: unparseable tool call ({type(tc).__name__})"
                tool_succeeded = False
                tool_result = None
            else:
                tool_call_id = tc.get("id")
                tool_name = tc.get("name")
                tool_obj = tool_by_name.get(tool_name) if tool_name else None
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
                        conversation_id=conversation_id,
                        tool_name=str(tool_name or "<missing>"),
                    )
                    tool_result_str = f"Error: unknown tool '{tool_name}'"
                    tool_succeeded = False
                    tool_result = None
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
                            conversation_id=conversation_id,
                            tool_name=str(tool_name or "<missing>"),
                            error_type=type(exc).__name__,
                        )
                        tool_result_str = f"Error: tool '{tool_name}' failed ({type(exc).__name__})"
                        tool_succeeded = False
                        tool_result = None
                    else:
                        tool_succeeded = True
                        tool_result_str = _tool_result_to_string(tool_result)

            # ---- Escalation short-circuit (M1 contract) ----
            # When ``escalate_to_human`` succeeds, the
            # conversation state has already been flipped at
            # the DB level by the tool's side effect — calling
            # the LLM again would only risk the model
            # overriding the escalation. We log any sibling
            # tool calls (the "extras ignored" M1 pattern) and
            # return the escalation result so the graph routes
            # to the escalation terminal.
            if tool_succeeded and tool_name == ESCALATION_TOOL_NAME:
                for extra in tool_calls[idx + 1 :]:
                    extra_name = extra.get("name") if isinstance(extra, dict) else "<unparsed>"
                    log.warning(
                        "agent.graph.extra_tool_calls_ignored",
                        tenant_id=tenant_id,
                        conversation_id=conversation_id,
                        tool_name=str(extra_name or "<missing>"),
                    )
                return (
                    {
                        "escalated": True,
                        "escalation_message": (
                            _tool_result_to_message(tool_result)
                            if tool_succeeded
                            else tool_result_str
                        ),
                        "tool_iterations": iterations,
                    },
                    iterations,
                    current_response,
                )

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
            request = build_request()
            current_response = await client.chat(request)
        except (
            RateLimited,
            ProviderUnavailable,
            OutputInvalid,
            InvalidRequest,
        ) as exc:
            log.warning(
                "agent.graph.llm_failed",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                error_type=type(exc).__name__,
            )
            return (
                {
                    "final_text": FALLBACK_MESSAGE,
                    "escalated": False,
                    "tool_iterations": iterations,
                },
                iterations,
                current_response,
            )
        except Exception as exc:
            # Defence-in-depth safety net — should NEVER fire
            # because the typed set above covers every documented
            # LLM-client error mode, but if a regression sneaks
            # in we refuse to crash the AI auto-reply.
            # Centralized log + fallback helper keeps the
            # bare-except shape consistent across the agent
            # graph. PII-safe: no exc_info, no repr, no
            # customer text.
            from agent.graph._safe import log_and_return

            terminal = log_and_return(
                fallback={
                    "final_text": FALLBACK_MESSAGE,
                    "escalated": False,
                    "tool_iterations": iterations,
                },
                event="agent.graph.llm_failed_unexpected",
                exc=exc,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )
            return (terminal, iterations, current_response)

    return None, iterations, current_response


__all__ = ["dispatch_tool_calls"]
