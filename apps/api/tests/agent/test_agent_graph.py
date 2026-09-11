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
from agent.graph.prompts import FALLBACK_MESSAGE, M1_SYSTEM_PROMPT
from agent.graph.state import AgentState
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
