"""Tests for the M1 LangChain + LangGraph agent graph (Stage 7.1).

These tests pin the contract for the minimal ``retrieve -> llm``
graph introduced in 7.1. The full SimpleResponder pipeline is
covered by ``test_simple_responder.py``; the graph tests focus
on the graph-only primitives — node behaviour, state schema, and
end-to-end flow with stubbed services.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.graph.graph import build_agent_graph
from agent.graph.nodes import make_llm_node, make_retrieve_node
from agent.graph.prompts import (
    CHAT_MAX_TOKENS,
    CHAT_TEMPERATURE,
    ESCALATION_TOOL_NAME,
    FALLBACK_MESSAGE,
    M1_SYSTEM_PROMPT,
)
from agent.graph.state import AgentState
from agent.graph.tools import make_escalate_tool
from agent.simple_responder import DEFAULT_MODEL
from knowledge.rag_service import RagContext
from llm_client.client import LLMClient

# ----- stubs (mirror the _empty_rag_service / _fake_client pattern) -----


def _empty_rag_context() -> RagContext:
    return RagContext(
        system_message="",
        chunk_count=0,
        knowledge_base_id="",
        knowledge_base_name="",
        retrieval_score_max=0.0,
    )


def _capturing_rag_service(*, chunks: list[str] | None = None) -> MagicMock:
    """RAG stub. When ``chunks`` is empty / ``None``, behaves like
    the no-RAG path. When ``chunks`` has entries, returns a context
    with the chunks concatenated into a ``system_message``.
    """
    svc = MagicMock()
    if chunks:
        text = "Retrieved knowledge:\n\n" + "\n".join(
            f"{i + 1}. {c}" for i, c in enumerate(chunks)
        )
        svc.build_context_for_query = AsyncMock(
            return_value=RagContext(
                system_message=text,
                chunk_count=len(chunks),
                knowledge_base_id="kb1",
                knowledge_base_name="kb1",
                retrieval_score_max=0.9,
            )
        )
    else:
        svc.build_context_for_query = AsyncMock(return_value=_empty_rag_context())
    return svc


class _StubChatResponse:
    """Minimal stand-in for ``llm_client.types.ChatResponse``.

    Avoids importing the pydantic model so the graph tests don't
    need the full LLM client machinery — they only care about
    ``.content``.
    """

    def __init__(self, *, content: str) -> None:
        self.content = content


def _capturing_llm_client(
    *,
    content: str = "hi",
    raises: BaseException | None = None,
) -> MagicMock:
    """LLM stub. Returns ``content`` (or raises ``raises``) on every
    ``chat`` call. Uses :class:`AsyncMock` so tests can introspect
    ``await_count`` / ``await_args`` for end-to-end assertions.
    """
    client = MagicMock()
    if raises is not None:
        client.chat = AsyncMock(side_effect=raises)
    else:
        client.chat = AsyncMock(return_value=_StubChatResponse(content=content))
    return client


def _make_factory(client: MagicMock) -> Callable[[str], LLMClient]:
    """Return a per-tenant LLMClient factory that always hands out
    ``client``. Defined as a ``def`` rather than a ``lambda`` to keep
    ruff's E731 happy.
    """

    def _factory(_tenant_id: str) -> LLMClient:
        return client

    return _factory


def _state(
    *,
    tenant_id: str = "t1",
    conversation_id: str = "c1",
    messages: list[Any] | None = None,
    rag_messages: list[Any] | None = None,
    final_text: str | None = None,
) -> AgentState:
    return AgentState(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        messages=list(messages or []),
        rag_messages=list(rag_messages or []),
        final_text=final_text,
    )


# ----- state schema -------------------------------------------------------


def test_agent_state_default_keys() -> None:
    """The TypedDict exposes exactly the documented keys."""
    s: AgentState = _state(
        messages=[HumanMessage(content="hi")],
    )
    assert set(s.keys()) == {
        "tenant_id",
        "conversation_id",
        "messages",
        "rag_messages",
        "final_text",
    }
    assert s["tenant_id"] == "t1"
    assert s["conversation_id"] == "c1"
    assert isinstance(s["messages"], list)
    assert isinstance(s["rag_messages"], list)
    assert s["final_text"] is None


# ----- retrieve_node ------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_node_no_rag_service() -> None:
    """Empty RAG context -> empty rag_messages list."""
    rag_service = _capturing_rag_service()
    node = make_retrieve_node(rag_service=rag_service)
    state = _state(messages=[HumanMessage(content="how do I reset password?")])

    result = await node(state)

    assert result == {"rag_messages": []}
    # The RAG service IS called (we want RAG attempts even when
    # they yield nothing) — the fail-open happens at the result
    # level, not at the call level.
    rag_service.build_context_for_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_retrieve_node_with_rag() -> None:
    """Non-empty RAG context -> SystemMessage in rag_messages."""
    rag_service = _capturing_rag_service(
        chunks=["Reset via Settings > Password", "Email support@example.com"]
    )
    node = make_retrieve_node(rag_service=rag_service)
    state = _state(messages=[HumanMessage(content="how do I reset password?")])

    result = await node(state)

    assert "rag_messages" in result
    assert len(result["rag_messages"]) == 1
    msg = result["rag_messages"][0]
    assert isinstance(msg, SystemMessage)
    assert "Reset via Settings" in msg.content


@pytest.mark.asyncio
async def test_retrieve_node_no_customer_message_returns_empty() -> None:
    """When there's no HumanMessage in messages, RAG is skipped entirely."""
    rag_service = _capturing_rag_service()
    node = make_retrieve_node(rag_service=rag_service)
    # Only an AI message — no customer turn to anchor the retrieval on.
    state = _state(messages=[AIMessage(content="hello")])

    result = await node(state)

    assert result == {"rag_messages": []}
    rag_service.build_context_for_query.assert_not_called()


@pytest.mark.asyncio
async def test_retrieve_node_exception_returns_empty() -> None:
    """RAG service raising MUST NOT propagate — the node fails open."""

    class _BoomRAG:
        async def build_context_for_query(self, **_kwargs: object) -> RagContext:
            raise RuntimeError("rag down")

    node = make_retrieve_node(rag_service=_BoomRAG())  # type: ignore[arg-type]
    state = _state(messages=[HumanMessage(content="hi")])

    result = await node(state)

    assert result == {"rag_messages": []}


# ----- llm_node -----------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_node_returns_text() -> None:
    """LLM returning content -> final_text populated."""
    client = _capturing_llm_client(content="hi")
    node = make_llm_node(llm_client_factory=_make_factory(client), model=DEFAULT_MODEL)
    state = _state(
        messages=[HumanMessage(content="hello")],
        rag_messages=[SystemMessage(content="rag ctx")],
    )

    result = await node(state)

    assert result == {"final_text": "hi"}


@pytest.mark.asyncio
async def test_llm_node_assembles_messages_in_order() -> None:
    """System prompt -> rag_messages -> messages is the documented order."""
    captured: dict[str, Any] = {}

    async def _capture(request: object) -> object:
        # request is a ChatRequest from llm_client.types; we only
        # touch .messages and .model.
        captured["messages"] = list(request.messages)  # type: ignore[attr-defined]
        captured["model"] = request.model  # type: ignore[attr-defined]
        captured["temperature"] = request.temperature  # type: ignore[attr-defined]
        captured["max_tokens"] = request.max_tokens  # type: ignore[attr-defined]
        return _StubChatResponse(content="ok")

    client = MagicMock()
    client.chat = _capture
    node = make_llm_node(llm_client_factory=_make_factory(client), model=DEFAULT_MODEL)

    state = _state(
        messages=[
            HumanMessage(content="user turn"),
            AIMessage(content="prior ai"),
        ],
        rag_messages=[SystemMessage(content="rag ctx")],
    )

    await node(state)

    msgs: list[Any] = captured["messages"]
    assert captured["model"] == DEFAULT_MODEL
    # First message is the M1 system prompt; rag ctx follows; then history.
    assert msgs[0].role == "system"
    assert M1_SYSTEM_PROMPT in msgs[0].content
    assert msgs[1].role == "system"
    assert msgs[1].content == "rag ctx"
    assert msgs[2].role == "user"
    assert msgs[3].role == "assistant"
    # CHAT_TEMPERATURE / CHAT_MAX_TOKENS must be applied to the
    # request — falling back to Pydantic defaults would drop the
    # max_tokens cap the M1 spec pinned.
    assert captured["temperature"] == CHAT_TEMPERATURE
    assert captured["max_tokens"] == CHAT_MAX_TOKENS


@pytest.mark.asyncio
async def test_llm_node_applies_chat_temperature_and_max_tokens() -> None:
    """Concern #3 regression guard: max_tokens MUST be pinned, not None."""
    captured: dict[str, Any] = {}

    async def _capture(request: object) -> object:
        captured["temperature"] = request.temperature  # type: ignore[attr-defined]
        captured["max_tokens"] = request.max_tokens  # type: ignore[attr-defined]
        return _StubChatResponse(content="ok")

    client = MagicMock()
    client.chat = _capture
    node = make_llm_node(llm_client_factory=_make_factory(client), model=DEFAULT_MODEL)

    await node(
        _state(messages=[HumanMessage(content="hi")])
    )

    assert captured["temperature"] == CHAT_TEMPERATURE
    # max_tokens must be a positive int, NOT the Pydantic default of None.
    # Without this, the LLM could spend unlimited tokens per reply.
    assert captured["max_tokens"] == CHAT_MAX_TOKENS
    assert isinstance(captured["max_tokens"], int)
    assert captured["max_tokens"] > 0


@pytest.mark.asyncio
async def test_llm_node_fallback_on_exception() -> None:
    """LLM raising -> final_text is FALLBACK_MESSAGE; node never raises."""
    client = _capturing_llm_client(raises=RuntimeError("api down"))
    node = make_llm_node(llm_client_factory=_make_factory(client), model=DEFAULT_MODEL)
    state = _state(messages=[HumanMessage(content="hi")])

    result = await node(state)

    assert result == {"final_text": FALLBACK_MESSAGE}


@pytest.mark.asyncio
async def test_llm_node_fallback_on_empty_content() -> None:
    """Empty LLM content -> FALLBACK_MESSAGE (defensive)."""
    client = _capturing_llm_client(content="   ")
    node = make_llm_node(llm_client_factory=_make_factory(client), model=DEFAULT_MODEL)
    state = _state(messages=[HumanMessage(content="hi")])

    result = await node(state)

    assert result == {"final_text": FALLBACK_MESSAGE}


# ----- build_agent_graph --------------------------------------------------


def test_build_agent_graph_returns_compiled() -> None:
    """``build_agent_graph`` returns an object with ``.ainvoke``."""
    rag_service = _capturing_rag_service()
    client = _capturing_llm_client()
    graph = build_agent_graph(
        rag_service=rag_service,
        llm_client_factory=_make_factory(client),
        model=DEFAULT_MODEL,
    )
    assert hasattr(graph, "ainvoke")
    assert callable(graph.ainvoke)


@pytest.mark.asyncio
async def test_end_to_end_graph_flow() -> None:
    """``ainvoke`` returns the expected final_text with stub services."""
    rag_service = _capturing_rag_service(
        chunks=["reset via settings"]
    )
    client = _capturing_llm_client(content="reset via settings panel")
    graph = build_agent_graph(
        rag_service=rag_service,
        llm_client_factory=_make_factory(client),
        model=DEFAULT_MODEL,
    )

    state = _state(messages=[HumanMessage(content="how do I reset?")])

    result = await graph.ainvoke(dict(state))

    assert result["final_text"] == "reset via settings panel"
    # The LLM was called exactly once, with the rag context prepended.
    assert client.chat.await_count == 1
    sent = client.chat.await_args.args[0]
    sent_messages = sent.messages
    # system (M1 prompt), system (rag), user (the customer turn)
    assert sent_messages[0].role == "system"
    assert M1_SYSTEM_PROMPT in sent_messages[0].content
    assert sent_messages[1].role == "system"
    assert "reset via settings" in sent_messages[1].content
    assert sent_messages[2].role == "user"
    assert sent_messages[2].content == "how do I reset?"


@pytest.mark.asyncio
async def test_end_to_end_graph_flow_llm_failure_yields_fallback() -> None:
    """End-to-end: LLM failure -> FALLBACK_MESSAGE comes back out."""
    rag_service = _capturing_rag_service()
    client = _capturing_llm_client(raises=RuntimeError("boom"))
    graph = build_agent_graph(
        rag_service=rag_service,
        llm_client_factory=_make_factory(client),
        model=DEFAULT_MODEL,
    )
    state = _state(messages=[HumanMessage(content="hi")])

    result = await graph.ainvoke(dict(state))

    assert result["final_text"] == FALLBACK_MESSAGE


@pytest.mark.asyncio
async def test_end_to_end_graph_flow_rag_failure_yields_no_rag_block() -> None:
    """End-to-end: RAG failure -> LLM still called without rag block."""

    class _BoomRAG:
        async def build_context_for_query(self, **_kwargs: object) -> RagContext:
            raise RuntimeError("rag down")

    client = _capturing_llm_client(content="ok")
    graph = build_agent_graph(
        rag_service=_BoomRAG(),  # type: ignore[arg-type]
        llm_client_factory=_make_factory(client),
        model=DEFAULT_MODEL,
    )
    state = _state(messages=[HumanMessage(content="hi")])

    result = await graph.ainvoke(dict(state))

    assert result["final_text"] == "ok"
    # Only the M1 system prompt + the user turn — no rag block.
    sent = client.chat.await_args.args[0]
    roles = [m.role for m in sent.messages]
    assert roles == ["system", "user"]


@pytest.mark.asyncio
async def test_end_to_end_rag_chunk_appears_exactly_once() -> None:
    """Concern #2 regression guard: retrieved chunks must NOT be duplicated.

    Pre-fix, the simple responder injected RAG into the ``messages``
    list *and* the graph's retrieve_node injected it again into
    ``rag_messages``. The LLM would then see the same chunk text
    twice in a single request. This test pins the dedup invariant
    by counting occurrences of a distinctive substring.
    """
    distinctive = "MARKER-RESET-INSTRUCTIONS-XYZ"
    rag_service = _capturing_rag_service(
        chunks=[distinctive, "Another chunk"]
    )
    client = _capturing_llm_client(content="ok")
    graph = build_agent_graph(
        rag_service=rag_service,
        llm_client_factory=_make_factory(client),
        model=DEFAULT_MODEL,
    )
    state = _state(
        tenant_id="t-iso",
        conversation_id="c-iso",
        messages=[HumanMessage(content="how do I reset?")],
    )

    await graph.ainvoke(dict(state))

    sent = client.chat.await_args.args[0]
    full_request = "\n".join(m.content for m in sent.messages)
    assert full_request.count(distinctive) == 1, (
        f"RAG chunk text appears more than once in the LLM request: "
        f"{full_request.count(distinctive)} occurrences"
    )


@pytest.mark.asyncio
async def test_retrieve_node_threads_tenant_id_to_rag_service() -> None:
    """Tenant isolation: retrieve_node forwards state['tenant_id'] to RAG."""
    rag_service = _capturing_rag_service()
    node = make_retrieve_node(rag_service=rag_service)
    state = _state(
        tenant_id="tenant-42",
        conversation_id="conv-7",
        messages=[HumanMessage(content="hi")],
    )

    await node(state)

    kwargs = rag_service.build_context_for_query.await_args.kwargs
    assert kwargs["tenant_id"] == "tenant-42"
    assert kwargs["conversation_id"] == "conv-7"
    assert kwargs["query"] == "hi"


@pytest.mark.asyncio
async def test_llm_node_threads_tenant_id_to_factory() -> None:
    """Tenant isolation: llm_node passes state['tenant_id'] to the factory."""
    captured_tenant: dict[str, str] = {}

    def _factory(tenant_id: str) -> LLMClient:
        captured_tenant["value"] = tenant_id
        return _capturing_llm_client(content="ok")

    node = make_llm_node(llm_client_factory=_factory, model=DEFAULT_MODEL)
    state = _state(
        tenant_id="tenant-99",
        messages=[HumanMessage(content="hi")],
    )

    await node(state)

    assert captured_tenant["value"] == "tenant-99"


# ----- Stage 7.2: escalate_to_human tool --------------------------------


class _ConvServiceSpy:
    """Spy for ``ConversationService.escalate_to_human_queue``.

    Records calls so tests can assert tenant / conversation ID
    binding without touching the real DB. ``escalate_to_human_queue``
    is the only surface the escalation tool calls, so spying on it
    is sufficient.
    """

    def __init__(self) -> None:
        self.escalate_calls: list[dict[str, Any]] = []

    async def escalate_to_human_queue(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
    ) -> Any:
        self.escalate_calls.append(
            {
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
            }
        )
        return None


def _fake_llm_client_with_tool_call(
    *,
    tool_calls: list[dict[str, Any]] | None,
    content: str = "",
) -> MagicMock:
    """LLM stub that returns a ``ChatResponse``-shaped object with
    ``tool_calls``. Used to drive the tool-dispatch path without
    hitting a real provider.
    """
    client = MagicMock()
    response = MagicMock()
    response.content = content
    response.tool_calls = tool_calls
    response.model = DEFAULT_MODEL
    response.prompt_tokens = 10
    response.completion_tokens = 5
    response.finish_reason = "tool_use"
    response.raw = {}
    client.chat = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_escalate_tool_factory_binds_tenant_and_conversation() -> None:
    """Tool's ``ainvoke`` MUST forward the bound
    ``tenant_id`` / ``conversation_id`` (read from the per-turn
    ``ContextVar``) to
    :meth:`ConversationService.escalate_to_human_queue`, not
    whatever the LLM claims in its tool arguments.

    Stage 7.4 — the tool factory no longer takes tenant / conv
    kwargs directly. The test binds them via
    :func:`bind_escalation_context` (the same entry point
    ``SimpleResponder.respond`` uses) and cleans up via
    :func:`reset_escalation_context`.
    """
    from agent.graph.tools import (
        bind_escalation_context,
        reset_escalation_context,
    )

    spy = _ConvServiceSpy()
    tool_obj = make_escalate_tool(conv_service=spy)  # type: ignore[arg-type]
    token = bind_escalation_context(
        tenant_id="t-escalate", conversation_id="c-escalate"
    )
    try:
        result = await tool_obj.ainvoke({"reason": "I need a human"})
    finally:
        reset_escalation_context(token)

    assert spy.escalate_calls == [
        {
            "tenant_id": "t-escalate",
            "conversation_id": "c-escalate",
        }
    ]
    assert result == {"escalated": True, "reason": "I need a human"}


@pytest.mark.asyncio
async def test_escalate_tool_returns_escalated_dict() -> None:
    """Tool return shape: ``{"escalated": True, "reason": ...}``.

    The ``summary`` arg is internal-only — must NOT appear in the
    returned payload (which becomes the customer-facing message).
    """
    from agent.graph.tools import (
        bind_escalation_context,
        reset_escalation_context,
    )

    spy = _ConvServiceSpy()
    tool_obj = make_escalate_tool(conv_service=spy)  # type: ignore[arg-type]
    token = bind_escalation_context(tenant_id="t1", conversation_id="c1")
    try:
        result = await tool_obj.ainvoke(
            {"reason": "Out of scope", "summary": "internal-only note"}
        )
    finally:
        reset_escalation_context(token)

    assert result == {"escalated": True, "reason": "Out of scope"}
    assert "summary" not in result


@pytest.mark.asyncio
async def test_llm_node_invokes_escalation_tool_on_tool_call() -> None:
    """When the LLM fires ``escalate_to_human``, the node MUST
    invoke the tool with the parsed args, set
    ``state["escalated"]=True``, and surface the customer-facing
    message in ``state["escalation_message"]``.

    Stage 7.4 — the test passes a pre-built ``tools=[tool_obj]``
    (so it bypasses the LLM node's per-turn ContextVar path)
    AND binds the ContextVar so the tool body resolves its
    tenant / conversation IDs.
    """
    from agent.graph.tools import (
        bind_escalation_context,
        reset_escalation_context,
    )

    spy = _ConvServiceSpy()
    tool_obj = make_escalate_tool(conv_service=spy)  # type: ignore[arg-type]
    token = bind_escalation_context(tenant_id="t1", conversation_id="c1")
    try:
        client = _fake_llm_client_with_tool_call(
            tool_calls=[
                {
                    "type": "tool_use",
                    "id": "toolu-1",
                    "name": ESCALATION_TOOL_NAME,
                    "input": {"reason": "Customer asked for a human"},
                }
            ],
        )
        node = make_llm_node(
            llm_client_factory=_make_factory(client),
            model=DEFAULT_MODEL,
            tools=[tool_obj],
        )
        state = _state(messages=[HumanMessage(content="transfer me to a human")])

        result = await node(state)
    finally:
        reset_escalation_context(token)

    assert result["escalated"] is True
    assert result["escalation_message"] == "Customer asked for a human"
    # Spy was invoked exactly once with the bound IDs.
    assert len(spy.escalate_calls) == 1


@pytest.mark.asyncio
async def test_llm_node_no_tool_call_sets_escalated_false() -> None:
    """A plain-text LLM response leaves ``escalated`` False and
    populates ``final_text`` from the LLM content (no merge key
    for ``escalated`` — state default is False)."""
    client = _capturing_llm_client(content="hi from llm")
    node = make_llm_node(
        llm_client_factory=_make_factory(client),
        model=DEFAULT_MODEL,
    )
    state = _state(messages=[HumanMessage(content="hi")])

    result = await node(state)

    assert result == {"final_text": "hi from llm"}
    # The node MUST NOT advertise ``escalated: False`` — that's
    # the state default and including it would couple the test
    # to internal LangGraph merge semantics.
    assert "escalated" not in result


@pytest.mark.asyncio
async def test_llm_node_tool_call_failure_writes_error_to_tool_message_and_continues_loop() -> None:
    """Stage 12 / Task 2 — if the tool raises, the node writes the
    error into the ToolMessage content and continues the loop. The
    LLM gets a second chance to produce a final answer. The
    customer turn MUST NOT crash.

    Pre-Task-2 the M1 contract was "fall back to the LLM's
    original text" — that path is gone now. The new behaviour
    matches the standard LangGraph tool-loop pattern: error in,
    LLM recovers, final text out.
    """
    class _BoomTool:
        name = ESCALATION_TOOL_NAME
        description = "boom"

        async def ainvoke(self, _args: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("tool exploded")

    # Mock returns the tool_call on the first call, then a final
    # text response after the loop surfaces the error.
    tool_call_response = MagicMock()
    tool_call_response.content = "fallback text"
    tool_call_response.tool_calls = [
        {
            "type": "tool_use",
            "id": "toolu-2",
            "name": ESCALATION_TOOL_NAME,
            "input": {"reason": "x"},
        }
    ]
    tool_call_response.model = DEFAULT_MODEL
    tool_call_response.prompt_tokens = 10
    tool_call_response.completion_tokens = 5
    tool_call_response.finish_reason = "tool_use"
    tool_call_response.raw = {}

    final_response = MagicMock()
    final_response.content = "recovered after error"
    final_response.tool_calls = []
    final_response.model = DEFAULT_MODEL
    final_response.prompt_tokens = 15
    final_response.completion_tokens = 10
    final_response.finish_reason = "stop"
    final_response.raw = {}

    client = MagicMock()
    client.chat = AsyncMock(side_effect=[tool_call_response, final_response])

    node = make_llm_node(
        llm_client_factory=_make_factory(client),
        model=DEFAULT_MODEL,
        tools=[_BoomTool()],  # type: ignore[list-item]
    )
    state = _state(messages=[HumanMessage(content="hi")])

    result = await node(state)

    # The LLM is called twice — once for the tool_call, once after
    # the loop surfaces the error in a ToolMessage and the model
    # recovers.
    assert client.chat.await_count == 2
    # The LLM's *second* response is what the customer sees; the
    # original ``fallback text`` content of the first response is
    # no longer the final answer.
    assert result == {"final_text": "recovered after error"}
    assert "escalated" not in result


@pytest.mark.asyncio
async def test_graph_routes_to_escalation_node_when_escalated() -> None:
    """End-to-end: ``ainvoke`` returns state with ``escalated=True``
    and the escalation message in ``final_text``.

    Stage 7.4 — the hoisted ``escalate_to_human`` tool reads its
    tenant / conversation IDs from the per-turn ``ContextVar``
    that ``SimpleResponder`` would normally bind. This test
    binds the ContextVar directly because it bypasses the
    responder.
    """
    from agent.graph.tools import (
        bind_escalation_context,
        reset_escalation_context,
    )

    spy = _ConvServiceSpy()
    client = _fake_llm_client_with_tool_call(
        tool_calls=[
            {
                "type": "tool_use",
                "id": "toolu-3",
                "name": ESCALATION_TOOL_NAME,
                "input": {"reason": "Escalation reason"},
            }
        ],
    )
    graph = build_agent_graph(
        rag_service=_capturing_rag_service(),
        llm_client_factory=_make_factory(client),
        model=DEFAULT_MODEL,
        conv_service=spy,  # type: ignore[arg-type]
    )
    state = _state(
        tenant_id="t-route",
        conversation_id="c-route",
        messages=[HumanMessage(content="transfer me")],
    )
    token = bind_escalation_context(
        tenant_id="t-route", conversation_id="c-route"
    )
    try:
        result = await graph.ainvoke(dict(state))
    finally:
        reset_escalation_context(token)

    assert result["escalated"] is True
    assert result["final_text"] == "Escalation reason"
    assert result["escalation_message"] == "Escalation reason"
    assert len(spy.escalate_calls) == 1


@pytest.mark.asyncio
async def test_graph_routes_to_end_when_not_escalated() -> None:
    """End-to-end: normal text path goes straight to END with
    ``escalated`` left at its default (False)."""
    rag_service = _capturing_rag_service()
    client = _capturing_llm_client(content="normal answer")
    graph = build_agent_graph(
        rag_service=rag_service,
        llm_client_factory=_make_factory(client),
        model=DEFAULT_MODEL,
    )
    state = _state(messages=[HumanMessage(content="hi")])

    result = await graph.ainvoke(dict(state))

    assert result["final_text"] == "normal answer"
    # LangGraph fills missing keys with None on the merged state;
    # the conditional edge treats None as falsy so the test below
    # documents the observable behaviour.
    assert not result.get("escalated")


@pytest.mark.asyncio
async def test_graph_escalation_node_is_trivial_passthrough() -> None:
    """Sanity: ``escalation_node`` copies ``escalation_message``
    into ``final_text`` — the graph topology makes that explicit
    even though it's a one-liner."""
    from agent.graph.nodes import make_escalation_node

    node = make_escalation_node()
    state: AgentState = {
        "tenant_id": "t1",
        "conversation_id": "c1",
        "messages": [],
        "rag_messages": [],
        "final_text": None,
        "escalated": True,
        "escalation_message": "Connecting you with a colleague",
    }

    result = await node(state)

    assert result == {"final_text": "Connecting you with a colleague"}


# ----- Stage 7.4: state-machine polish + metrics -------------------------


import contextlib
import io


class _StructlogCapture:
    """Captures structlog events emitted during a code block.

    The M1 logger config (``core.logging.configure_logging``)
    uses a ``PrintLoggerFactory`` that writes to ``sys.stdout``.
    We :func:`contextlib.redirect_stdout` to a buffer for the
    duration of the test, then expose the captured text so
    tests can assert on event names.

    Note: the test environment does NOT invoke
    ``configure_logging`` (that's a process-startup concern),
    so structlog falls back to its default ``ConsoleRenderer``
    which produces plain-text lines like
    ``2026-09-11 12:00:00 [info] event_name key=value``.
    We do NOT parse JSON; we just substring-match.
    """

    def __init__(self) -> None:
        self.buffer = io.StringIO()
        self.text: str = ""
        self._redirect_cm: Any = None

    def __enter__(self) -> _StructlogCapture:
        self._redirect_cm = contextlib.redirect_stdout(self.buffer)
        self._redirect_cm.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._redirect_cm is not None
        self._redirect_cm.__exit__(*exc)
        self.text = self.buffer.getvalue()

    def has_event(self, event_name: str) -> bool:
        """Return True if ``event_name`` appears as a token in
        any captured log line."""
        return event_name in self.text

    def count_event(self, event_name: str) -> int:
        """Return the number of captured lines that contain
        ``event_name`` as a token."""
        return sum(1 for line in self.text.splitlines() if event_name in line)


@pytest.mark.asyncio
async def test_retrieve_node_handles_none_messages() -> None:
    """Stage 7.4 — ``state["messages"] is None`` MUST NOT crash
    the retrieve node; it short-circuits to an empty rag list.
    """
    rag_service = _capturing_rag_service()
    node = make_retrieve_node(rag_service=rag_service)
    state: AgentState = {
        "tenant_id": "t1",
        "conversation_id": "c1",
        "messages": None,  # type: ignore[typeddict-item]
        "rag_messages": [],
        "final_text": None,
        "escalated": False,
        "escalation_message": None,
    }

    result = await node(state)

    assert result == {"rag_messages": []}
    # RAG service is NOT called when there are no messages to
    # anchor the query on — the node short-circuits earlier.
    rag_service.build_context_for_query.assert_not_called()


@pytest.mark.asyncio
async def test_llm_node_handles_multiple_tool_calls_first_wins() -> None:
    """Stage 7.4 — when the LLM returns 2+ tool calls, the node
    dispatches ONLY the first and logs a WARNING for the rest.
    """
    from agent.graph.tools import (
        bind_escalation_context,
        reset_escalation_context,
    )

    spy = _ConvServiceSpy()
    tool_obj = make_escalate_tool(conv_service=spy)  # type: ignore[arg-type]
    token = bind_escalation_context(tenant_id="t1", conversation_id="c1")
    try:
        client = _fake_llm_client_with_tool_call(
            tool_calls=[
                {
                    "type": "tool_use",
                    "id": "toolu-first",
                    "name": ESCALATION_TOOL_NAME,
                    "input": {"reason": "first wins"},
                },
                {
                    "type": "tool_use",
                    "id": "toolu-second",
                    "name": ESCALATION_TOOL_NAME,
                    "input": {"reason": "second ignored"},
                },
            ],
        )
        node = make_llm_node(
            llm_client_factory=_make_factory(client),
            model=DEFAULT_MODEL,
            tools=[tool_obj],
        )
        state = _state(messages=[HumanMessage(content="transfer me")])

        with _StructlogCapture() as capture:
            result = await node(state)
    finally:
        reset_escalation_context(token)

    assert result["escalated"] is True
    assert result["escalation_message"] == "first wins"
    # Exactly one escalation was performed — the second tool call
    # was logged but not dispatched.
    assert len(spy.escalate_calls) == 1
    # The "extras ignored" WARNING was emitted with the second
    # tool name.
    assert capture.has_event("agent.graph.extra_tool_calls_ignored")


@pytest.mark.asyncio
async def test_llm_node_handles_typed_llm_exceptions() -> None:
    """Stage 7.4 — typed LLM exceptions (``RateLimited``,
    ``ProviderUnavailable``) downgrade to ``FALLBACK_MESSAGE``
    with a WARNING + ``error_type``. The node never raises."""
    from llm_client.exceptions import ProviderUnavailable, RateLimited

    for exception_cls in (RateLimited, ProviderUnavailable):
        client = _capturing_llm_client(raises=exception_cls("boom"))
        node = make_llm_node(
            llm_client_factory=_make_factory(client),
            model=DEFAULT_MODEL,
        )
        state = _state(messages=[HumanMessage(content="hi")])

        with _StructlogCapture() as capture:
            result = await node(state)

        assert result == {"final_text": FALLBACK_MESSAGE}
        # The typed-catch path logged a WARNING carrying
        # ``error_type`` matching the exception class name.
        assert capture.has_event("agent.graph.llm_failed")
        assert exception_cls.__name__ in capture.text


@pytest.mark.asyncio
async def test_graph_emits_timing_metrics() -> None:
    """Stage 7.4 — ``build_agent_graph`` wraps ``ainvoke`` so each
    invocation emits ``agent.graph.invoke.started`` AND
    ``agent.graph.invoke.completed`` with ``duration_ms`` and
    ``turn_kind``.
    """
    rag_service = _capturing_rag_service()
    client = _capturing_llm_client(content="ok")
    graph = build_agent_graph(
        rag_service=rag_service,
        llm_client_factory=_make_factory(client),
        model=DEFAULT_MODEL,
    )
    state = _state(messages=[HumanMessage(content="hi")])

    with _StructlogCapture() as capture:
        await graph.ainvoke(dict(state))

    assert capture.has_event("agent.graph.invoke.started")
    assert capture.has_event("agent.graph.invoke.completed")
    # The completed event carries duration_ms + turn_kind.
    completed_lines = [
        line for line in capture.text.splitlines()
        if "agent.graph.invoke.completed" in line
    ]
    assert len(completed_lines) == 1
    assert "duration_ms" in completed_lines[0]
    assert "turn_kind" in completed_lines[0]
    assert "no_rag" in completed_lines[0]


@pytest.mark.asyncio
async def test_graph_emits_timing_metrics_with_rag() -> None:
    """Stage 7.4 — RAG-hitting turn is classified ``rag_hit``."""
    rag_service = _capturing_rag_service(chunks=["chunk-1"])
    client = _capturing_llm_client(content="ok")
    graph = build_agent_graph(
        rag_service=rag_service,
        llm_client_factory=_make_factory(client),
        model=DEFAULT_MODEL,
    )
    state = _state(messages=[HumanMessage(content="hi")])

    with _StructlogCapture() as capture:
        await graph.ainvoke(dict(state))

    completed_lines = [
        line for line in capture.text.splitlines()
        if "agent.graph.invoke.completed" in line
    ]
    assert len(completed_lines) == 1
    assert "rag_hit" in completed_lines[0]


@pytest.mark.asyncio
async def test_graph_emits_timing_metrics_with_escalation() -> None:
    """Stage 7.4 — escalated turn is classified ``escalated``."""
    from agent.graph.tools import (
        bind_escalation_context,
        reset_escalation_context,
    )

    spy = _ConvServiceSpy()
    client = _fake_llm_client_with_tool_call(
        tool_calls=[
            {
                "type": "tool_use",
                "id": "toolu-metric",
                "name": ESCALATION_TOOL_NAME,
                "input": {"reason": "metric reason"},
            }
        ],
    )
    graph = build_agent_graph(
        rag_service=_capturing_rag_service(),
        llm_client_factory=_make_factory(client),
        model=DEFAULT_MODEL,
        conv_service=spy,  # type: ignore[arg-type]
    )
    state = _state(
        tenant_id="t-metric",
        conversation_id="c-metric",
        messages=[HumanMessage(content="transfer me")],
    )
    token = bind_escalation_context(
        tenant_id="t-metric", conversation_id="c-metric"
    )
    try:
        with _StructlogCapture() as capture:
            await graph.ainvoke(dict(state))
    finally:
        reset_escalation_context(token)

    completed_lines = [
        line for line in capture.text.splitlines()
        if "agent.graph.invoke.completed" in line
    ]
    assert len(completed_lines) == 1
    assert "escalated" in completed_lines[0]


@pytest.mark.asyncio
async def test_llm_node_streams_deltas_when_on_delta_provided() -> None:
    """When state carries an ``on_delta`` callback, the node must use
    ``stream_chat`` and relay each text chunk; the final response still
    populates ``final_text`` (and its ``tool_calls`` feed escalation).
    """
    from llm_client.types import ChatResponse

    final = ChatResponse(
        content="Hello there",
        model="mini",
        prompt_tokens=4,
        completion_tokens=5,
        finish_reason="stop",
    )

    async def _stream(_request):
        yield "Hel"
        yield "lo"
        yield final

    client = MagicMock()
    client.stream_chat = _stream

    deltas_received: list[str] = []

    async def on_delta(text: str) -> None:
        deltas_received.append(text)

    node = make_llm_node(llm_client_factory=_make_factory(client), model=DEFAULT_MODEL)
    state = _state(messages=[HumanMessage(content="hi")])
    state["on_delta"] = on_delta  # type: ignore[typeddict-unknown-key]

    result = await node(state)

    assert result == {"final_text": "Hello there"}
    assert deltas_received == ["Hel", "lo"]
    # stream_chat was used (chat must NOT be invoked on the streaming path)
    assert client.chat.called is False


@pytest.mark.asyncio
async def test_llm_node_streams_tool_escalation_from_final_response() -> None:
    """A streamed turn that ends in a tool call must still escalate — the
    reassembled ``tool_calls`` on the final ``ChatResponse`` drive dispatch.
    """
    from llm_client.types import ChatResponse

    final = ChatResponse(
        content="",
        model="mini",
        prompt_tokens=4,
        completion_tokens=5,
        finish_reason="tool_calls",
        tool_calls=[
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "escalate_to_human",
                "input": {"reason": "streamed"},
            }
        ],
    )

    async def _stream(_request):
        yield final

    client = MagicMock()
    client.stream_chat = _stream

    async def on_delta(_text: str) -> None:
        pass

    from agent.graph.tools import (
        bind_escalation_context,
        reset_escalation_context,
    )

    tool_obj = MagicMock()
    tool_obj.name = "escalate_to_human"
    tool_obj.ainvoke = AsyncMock(return_value="streamed escalation")

    node = make_llm_node(
        llm_client_factory=_make_factory(client),
        model=DEFAULT_MODEL,
        tools=[tool_obj],
    )
    state = _state(messages=[HumanMessage(content="transfer me")])
    state["on_delta"] = on_delta  # type: ignore[typeddict-unknown-key]

    token = bind_escalation_context(tenant_id="t1", conversation_id="c1")
    try:
        result = await node(state)
    finally:
        reset_escalation_context(token)

    assert result["escalated"] is True
    assert result["escalation_message"] == "streamed escalation"
    tool_obj.ainvoke.assert_awaited_once()
