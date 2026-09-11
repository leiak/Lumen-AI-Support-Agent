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
    """Spy for ``ConversationService.assign_to_agent``.

    Records calls so tests can assert tenant / conversation ID
    binding without touching the real DB. ``assign_to_agent`` is
    the only surface the escalation tool calls, so spying on it
    is sufficient.
    """

    def __init__(self) -> None:
        self.assign_calls: list[dict[str, Any]] = []

    async def assign_to_agent(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        agent_id: Any,
    ) -> Any:
        self.assign_calls.append(
            {
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "agent_id": agent_id,
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
    """Tool's ``ainvoke`` MUST forward the closed-over
    ``tenant_id`` / ``conversation_id`` to
    :meth:`ConversationService.assign_to_agent`, not whatever the
    LLM claims in its tool arguments."""
    spy = _ConvServiceSpy()
    tool_obj = make_escalate_tool(
        tenant_id="t-escalate",
        conversation_id="c-escalate",
        conv_service=spy,  # type: ignore[arg-type]
    )

    result = await tool_obj.ainvoke({"reason": "I need a human"})

    assert spy.assign_calls == [
        {
            "tenant_id": "t-escalate",
            "conversation_id": "c-escalate",
            "agent_id": None,
        }
    ]
    assert result == {"escalated": True, "reason": "I need a human"}


@pytest.mark.asyncio
async def test_escalate_tool_returns_escalated_dict() -> None:
    """Tool return shape: ``{"escalated": True, "reason": ...}``.

    The ``summary`` arg is internal-only — must NOT appear in the
    returned payload (which becomes the customer-facing message).
    """
    spy = _ConvServiceSpy()
    tool_obj = make_escalate_tool(
        tenant_id="t1",
        conversation_id="c1",
        conv_service=spy,  # type: ignore[arg-type]
    )

    result = await tool_obj.ainvoke(
        {"reason": "Out of scope", "summary": "internal-only note"}
    )

    assert result == {"escalated": True, "reason": "Out of scope"}
    assert "summary" not in result


@pytest.mark.asyncio
async def test_llm_node_invokes_escalation_tool_on_tool_call() -> None:
    """When the LLM fires ``escalate_to_human``, the node MUST
    invoke the tool with the parsed args, set
    ``state["escalated"]=True``, and surface the customer-facing
    message in ``state["escalation_message"]``."""
    spy = _ConvServiceSpy()
    tool_obj = make_escalate_tool(
        tenant_id="t1",
        conversation_id="c1",
        conv_service=spy,  # type: ignore[arg-type]
    )
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

    assert result["escalated"] is True
    assert result["escalation_message"] == "Customer asked for a human"
    # Spy was invoked exactly once with the bound IDs.
    assert len(spy.assign_calls) == 1


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
async def test_llm_node_tool_call_failure_falls_back_to_text() -> None:
    """If the tool raises, the node logs a WARNING and falls back
    to the LLM's normal text response. The customer turn MUST
    NOT crash."""
    class _BoomTool:
        name = ESCALATION_TOOL_NAME
        description = "boom"

        async def ainvoke(self, _args: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("tool exploded")

    client = _fake_llm_client_with_tool_call(
        tool_calls=[
            {
                "type": "tool_use",
                "id": "toolu-2",
                "name": ESCALATION_TOOL_NAME,
                "input": {"reason": "x"},
            }
        ],
        content="fallback text",
    )
    node = make_llm_node(
        llm_client_factory=_make_factory(client),
        model=DEFAULT_MODEL,
        tools=[_BoomTool()],  # type: ignore[list-item]
    )
    state = _state(messages=[HumanMessage(content="hi")])

    result = await node(state)

    assert result == {"final_text": "fallback text"}
    assert "escalated" not in result


@pytest.mark.asyncio
async def test_graph_routes_to_escalation_node_when_escalated() -> None:
    """End-to-end: ``ainvoke`` returns state with ``escalated=True``
    and the escalation message in ``final_text``."""
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

    result = await graph.ainvoke(dict(state))

    assert result["escalated"] is True
    assert result["final_text"] == "Escalation reason"
    assert result["escalation_message"] == "Escalation reason"
    assert len(spy.assign_calls) == 1


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
