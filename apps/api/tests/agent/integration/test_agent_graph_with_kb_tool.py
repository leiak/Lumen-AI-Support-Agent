"""Stage 12 / Task 3 — integration test for ``search_internal_kb`` through the
LangGraph tool loop.

This is the *integration* companion to the four unit tests in
``tests/agent/graph/test_search_internal_kb_tool.py``. The unit tests
pin the tool's contract (top_k, kb_slug, context binding) in isolation;
this test pins the **end-to-end tool-loop wiring** — the LLM emits a
``search_internal_kb`` tool_call, the node dispatches it through the
real ``make_search_internal_kb_tool`` factory, feeds the result back
to the LLM, and the LLM produces a final answer.

Mocking strategy
----------------

We mock only at three boundaries (the standard seam):

1. ``llm_client_factory`` → scripted LLMClient that returns a tool call
   on call 1 and a final text answer on call 2.
2. ``KnowledgeBaseRepository.find_by_slug`` → MagicMock returning a
   fake KB row (so the tool resolves ``kb_slug="support"`` to a KB id).
3. ``RAGService.retrieve`` → MagicMock returning a deterministic list
   of chunks (so the tool's Markdown output is assertion-friendly).

Everything else is real: ``make_llm_node``, ``make_search_internal_kb_tool``,
the tool-loop dispatch logic, the ContextVar binding, and the
``tool_iterations`` accounting.

PII discipline
--------------

* Test data uses ``MAGIC_PHRASE_*`` markers and opaque IDs only.
* We never assert on customer message text (the customer asks "How
  do I reset my password?" but assertions target the LLM's scripted
  final reply, not the customer turn).
* The KB chunk content is a ``MAGIC_PHRASE_KB_CHUNK_*`` marker.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import HumanMessage

from agent.graph.nodes import make_llm_node
from agent.graph.state import AgentState
from agent.graph.tools import (
    bind_escalation_context,
    make_search_internal_kb_tool,
    reset_escalation_context,
)
from llm_client.types import ChatResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(*, tenant_id: str, conversation_id: str) -> AgentState:
    """Build the minimal ``AgentState`` the LLM node reads.

    Only the fields the LLM node consults are populated; the LLM node
    seeds ``tool_iterations`` from ``int(state.get("tool_iterations") or 0)``
    so the default ``0`` is the "first turn" sentinel.
    """
    return AgentState(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        messages=[HumanMessage(content="How do I reset my password?")],
        rag_messages=[],
        final_text=None,
        escalated=False,
        escalation_message=None,
        tool_iterations=0,
    )


class _ScriptedLLM:
    """Stub ``LLMClient`` that returns a tool call on the first chat
    invocation and a plain text reply on the second.

    Implements only the surface the LLM node touches: ``chat``. We
    don't stub ``stream_chat`` because the state passed to the node
    has no ``on_delta`` callback, so the node takes the non-streaming
    branch.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.calls: list[list[Any]] = []

    async def chat(self, request: Any, **_kwargs: Any) -> ChatResponse:
        self.call_count += 1
        self.calls.append(list(request.messages))

        if self.call_count == 1:
            # First LLM call: emit a tool_call for ``search_internal_kb``.
            # The provider-agnostic helper in ``nodes.py`` accepts both
            # the Anthropic ``input`` shape and the OpenAI ``args`` shape;
            # we use the OpenAI shape here so the test also exercises
            # ``_extract_tool_args``'s fallback path.
            return ChatResponse(
                content="",
                model=request.model,
                prompt_tokens=10,
                completion_tokens=5,
                finish_reason="tool_use",
                tool_calls=[
                    {
                        "type": "tool_use",
                        "id": "toolu-stub-search-kb",
                        "name": "search_internal_kb",
                        # OpenAI-style ``args`` (not Anthropic ``input``) —
                        # exercises ``_extract_tool_args`` fallback branch.
                        "args": {
                            "query": "reset password",
                            "kb_slug": "support",
                            "top_k": 3,
                        },
                    }
                ],
            )

        # Second LLM call: produce the final answer based on the tool
        # result that was appended to the message list.
        return ChatResponse(
            content="MAGIC_PHRASE_FINAL_ANSWER_DO_X",
            model=request.model,
            prompt_tokens=20,
            completion_tokens=10,
            finish_reason="stop",
            tool_calls=None,
        )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_internal_kb_tool_invoked_through_graph_loop() -> None:
    """End-to-end: ``make_llm_node`` dispatches ``search_internal_kb``
    via the real tool factory, the tool hits the mocked KB repo +
    RAG retrieve, the result is fed back to the LLM, and the LLM's
    final text becomes ``final_text``.

    Asserts:
      * ``final_text`` is the LLM's second-call content
      * ``escalated`` is False (search_internal_kb is not terminal)
      * ``tool_iterations == 1`` (one tool dispatch + one LLM final)
      * The tool saw the right ``kb_slug`` and ``top_k`` from the LLM
      * The KB repo's ``find_by_slug`` was called with the tenant_id
        from the ContextVar (not the LLM-supplied args)
      * The RAG retrieve was called with the right ``tenant_id``,
        ``knowledge_base_id``, and ``top_k``
    """
    # ---- Mocks at the three boundaries -----------------------------
    tenant_id = "t-test-kb-tool"
    conversation_id = "c-test-kb-tool"

    kb_repo = MagicMock()
    # ``find_by_slug`` returns a fake KB row. The tool only reads
    # ``.id`` off the result, so a MagicMock with ``.id`` set is
    # sufficient — but a plain dict-shaped attribute keeps the
    # assertion on ``call_args`` readable.
    fake_kb = MagicMock()
    fake_kb.id = "kb-stub-id"
    kb_repo.find_by_slug = AsyncMock(return_value=fake_kb)

    rag_service = MagicMock()
    rag_service.retrieve = AsyncMock(
        return_value=[
            {
                "chunk_id": "MAGIC_PHRASE_KB_CHUNK_001",
                "article_title": "MAGIC_PHRASE_KB_ARTICLE_TITLE",
                "score": 0.91,
                "content": "MAGIC_PHRASE_KB_CHUNK_BODY",
            },
        ]
    )

    # ---- Real factories ------------------------------------------
    search_kb_tool = make_search_internal_kb_tool(
        rag_service=rag_service,
        kb_repository=kb_repo,
    )
    llm = _ScriptedLLM()
    factory = MagicMock(return_value=llm)

    node = make_llm_node(
        llm_client_factory=factory,
        model="test-model",
        tools=[search_kb_tool],
    )

    # ---- Bind tenant context (the SimpleResponder does this) -----
    token = bind_escalation_context(
        tenant_id=tenant_id, conversation_id=conversation_id
    )
    try:
        result = await node(_state(
            tenant_id=tenant_id, conversation_id=conversation_id
        ))
    finally:
        reset_escalation_context(token)

    # ---- Assertions on the LLM-node return shape -------------------
    # M1 contract: ``final_text`` carries the LLM's final answer,
    # ``escalated`` stays False (search_internal_kb is not terminal),
    # ``tool_iterations`` increments once per successful dispatch.
    assert result["final_text"] == "MAGIC_PHRASE_FINAL_ANSWER_DO_X"
    # ``make_llm_node`` always writes ``escalated`` on every return
    # path — ``False`` on the normal text / fallback paths, ``True``
    # on the escalation short-circuit. The contract is a stable
    # shape so callers can rely on the key being present.
    assert result["escalated"] is False
    assert result["tool_iterations"] == 1, (
        f"expected exactly one tool dispatch; got "
        f"tool_iterations={result['tool_iterations']}"
    )
    # ``escalation_message`` stays at its default — search_internal_kb
    # never sets it. Absent on the normal text path; explicit None on
    # the escalation branch. Either way: falsy.
    assert not result.get("escalation_message")

    # ---- LLM was called twice (tool_call + final answer) ----------
    assert llm.call_count == 2, (
        f"expected exactly 2 LLM calls (tool_call + final), got "
        f"{llm.call_count}"
    )

    # ---- The KB repo's ``find_by_slug`` saw the right args ---------
    # Critical: tenant_id must come from the ContextVar (set by
    # SimpleResponder before invoking the graph), NOT from the LLM's
    # tool_call args. This is the cross-tenant isolation contract.
    kb_repo.find_by_slug.assert_awaited_once_with(tenant_id, "support")

    # ---- The RAG ``retrieve`` saw the right args ------------------
    rag_service.retrieve.assert_awaited_once()
    _, rag_kwargs = rag_service.retrieve.call_args
    assert rag_kwargs["tenant_id"] == tenant_id
    assert rag_kwargs["conversation_id"] == conversation_id
    assert rag_kwargs["knowledge_base_id"] == "kb-stub-id"
    assert rag_kwargs["top_k"] == 3
    # Query was the LLM-supplied query, trimmed.
    assert rag_kwargs["query"] == "reset password"

    # ---- The second LLM call's message list saw the tool result --
    # The tool's Markdown result was appended as a TOOL-role
    # ChatMessage before the second chat invocation. Verify the
    # LLM saw the chunk marker, which is the round-trip contract:
    # the LLM can answer based on what the tool returned.
    second_call_messages = llm.calls[1]
    tool_messages = [m for m in second_call_messages if m.role == "tool"]
    assert len(tool_messages) == 1, (
        f"expected exactly 1 TOOL message in the 2nd LLM call; "
        f"got {len(tool_messages)} (roles: "
        f"{[m.role for m in second_call_messages]!r})"
    )
    tool_content = tool_messages[0].content
    assert "MAGIC_PHRASE_KB_ARTICLE_TITLE" in tool_content, (
        f"tool result did not surface KB chunk to LLM; got "
        f"{tool_content[:200]!r}"
    )
    assert "MAGIC_PHRASE_KB_CHUNK_001" in tool_content