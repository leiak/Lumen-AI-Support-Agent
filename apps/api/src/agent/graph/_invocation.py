"""LLM invocation helpers extracted from ``agent.graph.nodes``.

``make_llm_node`` accumulated 500+ lines over Stages 7-12 — message
assembly, client invocation, tool dispatch, escalation short-circuit
all lived in one closure. This module peels off the *pure*
(LLM-client-free, async-free) pieces so they can be unit-tested
without spinning up a fake provider.

Why a separate module (not a private nested function)?
------------------------------------------------------

Nested-function extraction is tempting but loses easy unit testability:
``make_llm_node`` is a factory whose returned closure carries
``llm_client_factory``, ``model``, ``conv_service``, etc. The closure
body was unreachable without first calling the factory and stubbing
the LLM client. Pulling the helpers into a sibling module gives tests
direct import access while keeping the closure itself shorter.

Sibling module
--------------

``_tool_dispatch.py`` covers the async, stateful half (tool
loop, escalation short-circuit). Together they decompose the
500+ line closure into three pieces that each fit in one head:

* ``assemble_llm_messages`` (this module) — pure message order.
* ``make_llm_node`` (nodes.py) — I/O orchestration + state routing.
* ``dispatch_tool_calls`` (``_tool_dispatch.py``) — tool loop.
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import BaseMessage

from llm_client.types import ChatMessage as LLMChatMessage
from llm_client.types import MessageRole


def assemble_llm_messages(
    *,
    system_prompt: str,
    rag_messages: Sequence[BaseMessage],
    messages: Sequence[BaseMessage],
) -> list[LLMChatMessage]:
    """Build the ``ChatMessage`` list sent to :meth:`LLMClient.chat`.

    Order is::

        [SYSTEM(system_prompt),
         *SYSTEM(rag_messages via _to_llm_chat_message),
         *<history> via _to_llm_chat_message]

    The leading system prompt is constant per graph build. The
    ``rag_messages`` are the synthetic ``SystemMessage`` rows the
    retrieve node emits; passing them through
    ``_to_llm_chat_message`` keeps role mapping identical to the
    in-place version (a ``SystemMessage`` always becomes a
    ``MessageRole.SYSTEM`` row). The conversation history then
    flows in *after* the RAG block, preserving the M1 contract that
    the customer's most recent turn is the last row before the
    LLM is invoked.

    Why a kwarg-only signature?
    ---------------------------

    The three inputs are easy to swap in a positional signature and
    the order is otherwise semantically meaningless. Keyword-only
    forces callers to spell out which is which and keeps the call
    sites (which there are only two: ``make_llm_node`` and tests)
    auditable.

    Why a list (not ``Sequence``) return?
    -------------------------------------

    The downstream consumer (``_build_request`` and the tool
    loop) append ``ToolMessage`` rows to the returned list. A
    mutable ``list[LLMChatMessage]`` is the only type that
    supports that without a copy.
    """
    out: list[LLMChatMessage] = [
        LLMChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
    ]
    # ``_to_llm_chat_message`` lives in ``nodes.py`` and lives there
    # for historical reasons (Stage 7.1 — the adapter predates the
    # helper extraction). Re-importing inside the function body
    # would risk a circular import at module load time; the public
    # symbol is stable so a top-level import is safe.
    from agent.graph.nodes import _to_llm_chat_message

    out.extend(_to_llm_chat_message(m) for m in rag_messages)
    out.extend(_to_llm_chat_message(m) for m in messages)
    return out


__all__ = ["assemble_llm_messages"]
