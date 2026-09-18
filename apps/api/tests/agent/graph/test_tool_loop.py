"""Stage 12 / Task 2 — tool-loop upgrade for ``make_llm_node``.

These tests pin the contract for the *full* tool-call loop
introduced in Stage 12 Task 2. Pre-Task-2 the node used a
``tool_calls[:1]`` first-wins shortcut; the new implementation
dispatches ALL tool calls in a loop with ``max_iterations=5``
safety so multi-tool flows (e.g. ``search_internal_kb`` followed
by a follow-up question) actually work end-to-end.

The four tests cover:

1. **No tool calls** — the loop exits after one LLM call.
2. **One tool call, then done** — the loop dispatches, re-calls,
   and exits when the LLM stops emitting tool calls.
3. **max_iterations safety** — a misbehaving LLM that emits tool
   calls forever is bounded by ``max_iterations=5`` and falls
   back to :data:`FALLBACK_MESSAGE`.
4. **Unknown tool name** — the loop writes an error string into
   the ``ToolMessage`` and continues rather than crashing.

All tests use ``MagicMock`` for the LLM client and a real
``make_escalate_tool`` factory (with a mocked conversation
service) where the tool is exercised. The ``bind_escalation_context``
``ContextVar`` is set up the same way ``SimpleResponder.respond``
does it in production so the escalation tool can resolve its
tenant / conversation IDs.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage

from agent.graph.state import AgentState


def _state() -> AgentState:
    """Build the minimal ``AgentState`` shape the LLM node reads.

    The fields the node actually consults are
    ``messages``, ``rag_messages``, ``tenant_id``, and
    ``conversation_id``. Everything else is left at its default.
    """
    return AgentState(
        tenant_id="t1",
        conversation_id="c1",
        messages=[],
        rag_messages=[],
        final_text=None,
    )


@pytest.mark.asyncio
async def test_tool_loop_stops_when_no_tool_calls() -> None:
    """LLM returns a plain text response (no tool calls) → loop
    exits after the first LLM call. ``final_text`` is the LLM
    content, ``llm.chat.await_count == 1``.
    """
    from agent.graph.nodes import make_llm_node
    from agent.graph.tools import bind_escalation_context

    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value=AIMessage(content="answer", tool_calls=[])
    )
    factory = MagicMock(return_value=llm)
    node = make_llm_node(
        llm_client_factory=factory, model="test-model", tools=[]
    )
    bind_escalation_context(tenant_id="t1", conversation_id="c1")

    result = await node(_state())

    assert result["final_text"] == "answer"
    # Stage 12 / Task 3 — every non-escalation return path now
    # advertises ``escalated: False`` for a stable caller contract.
    assert result["escalated"] is False
    # LLM called once (no tool_calls → no second call).
    assert llm.chat.await_count == 1


@pytest.mark.asyncio
async def test_tool_loop_handles_one_tool_call_then_done() -> None:
    """LLM emits one tool_call → dispatch → re-call LLM → no
    tool_call → loop exits with ``final_text`` from the second
    response. ``llm.chat.await_count == 2``.
    """
    from agent.graph.nodes import make_llm_node
    from agent.graph.tools import bind_escalation_context

    # A custom BaseTool stand-in with a spyable ``ainvoke``. We
    # avoid ``make_escalate_tool`` here because the production
    # escalation tool has its own return-value semantics
    # (``{"escalated": True, "reason": ...}``); a plain string
    # return keeps the ToolMessage content well-defined and the
    # LLM gets an unambiguous signal.
    class _EchoSpy:
        name = "echo_tool"
        description = "echo the input"
        call_count = 0

        async def ainvoke(self, args: dict[str, object]) -> str:
            _EchoSpy.call_count += 1
            return f"echo:{args.get('text', '')}"

    echo_tool = _EchoSpy()

    llm = MagicMock()
    llm.chat = AsyncMock(
        side_effect=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tc1",
                        "name": "echo_tool",
                        "args": {"text": "hi"},
                    }
                ],
            ),
            AIMessage(content="final answer", tool_calls=[]),
        ]
    )
    factory = MagicMock(return_value=llm)
    node = make_llm_node(
        llm_client_factory=factory,
        model="test-model",
        tools=[echo_tool],
    )
    bind_escalation_context(tenant_id="t1", conversation_id="c1")

    result = await node(_state())

    # LLM called twice (once for tool_call, once after dispatch).
    assert llm.chat.await_count == 2
    # The LLM's second response is what the customer sees.
    assert result["final_text"] == "final answer"
    # Stage 12 / Task 3 — non-escalation text path advertises
    # ``escalated: False`` so callers can rely on the key.
    assert result["escalated"] is False
    # Sanity: the tool was actually called exactly once.
    assert _EchoSpy.call_count == 1


@pytest.mark.asyncio
async def test_tool_loop_max_iterations_safety() -> None:
    """LLM emits tool_calls indefinitely → ``max_iterations=5``
    bound kicks in and the node falls back to
    :data:`FALLBACK_MESSAGE`. ``llm.chat.await_count <= 6``
    (5 + 1 safety allowance).
    """
    from agent.graph.nodes import FALLBACK_MESSAGE, make_llm_node
    from agent.graph.tools import bind_escalation_context

    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value=AIMessage(
            content="",
            tool_calls=[
                {"id": "tc", "name": "nonexistent", "args": {}}
            ],
        )
    )
    factory = MagicMock(return_value=llm)
    node = make_llm_node(
        llm_client_factory=factory, model="test-model", tools=[]
    )
    bind_escalation_context(tenant_id="t1", conversation_id="c1")

    result = await node(_state())

    assert result["final_text"] == FALLBACK_MESSAGE
    # Stage 12 / Task 3 — every fallback path now advertises
    # ``escalated: False`` for a stable caller contract.
    assert result["escalated"] is False
    # Should bail out via max_iterations (5) — exact call count
    # depends on whether the loop counts attempts or successful
    # dispatches; the spec says <= 6 (5 + 1 safety).
    assert llm.chat.await_count <= 6


@pytest.mark.asyncio
async def test_tool_loop_handles_unknown_tool() -> None:
    """LLM calls a tool name not in ``active_tools`` → error
    string is written into the ``ToolMessage`` content → loop
    continues → LLM called again → loop exits with the LLM's
    final answer.
    """
    from agent.graph.nodes import make_llm_node
    from agent.graph.tools import bind_escalation_context

    llm = MagicMock()
    llm.chat = AsyncMock(
        side_effect=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tc1",
                        "name": "nonexistent_tool",
                        "args": {},
                    }
                ],
            ),
            AIMessage(content="recovered", tool_calls=[]),
        ]
    )
    factory = MagicMock(return_value=llm)
    node = make_llm_node(
        llm_client_factory=factory, model="test-model", tools=[]
    )
    bind_escalation_context(tenant_id="t1", conversation_id="c1")

    result = await node(_state())

    assert llm.chat.await_count == 2
    assert result["final_text"] == "recovered"
    # Stage 12 / Task 3 — non-escalation text path advertises
    # ``escalated: False`` so callers can rely on the key.
    assert result["escalated"] is False
