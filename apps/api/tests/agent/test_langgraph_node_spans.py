"""M6.4 — verify LangGraph node + tool-call spans.

These tests cover the manual ``tracer.start_as_current_span(...)``
wraps added in ``apps/api/src/agent/graph/nodes.py`` and
``apps/api/src/agent/graph/_tool_dispatch.py`` during the M6.4
``feat(otel): M6.4 agent + channel + ticket + admin spans`` ship.

The conftest at ``apps/api/tests/observability/conftest.py``
installs the in-memory span exporter at module-load time; this
test file re-exports the ``memory_exporter`` fixture via
``apps/api/tests/agent/conftest.py`` so we can inspect spans produced
by ``graph.ainvoke(...)`` and the tool dispatch loop.

PII discipline: span attributes asserted here (``tenant_id``,
``conversation.id``) are all on the ``PII_WHITELIST`` in
``observability/tracing.py``. Message bodies, tool args, KB chunk
text are deliberately NOT attached to any span.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.graph.graph import build_agent_graph
from agent.graph.state import AgentState
from agent.simple_responder import DEFAULT_MODEL
from agent.graph.tools import (
    bind_escalation_context,
    reset_escalation_context,
)
from langchain_core.messages import HumanMessage
from llm_client.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers (mirror test_agent_graph.py's stub pattern)
# ---------------------------------------------------------------------------


class _StubChatResponse:
    """Minimal :class:`ChatResponse` stand-in for the LLM stub.

    The graph code only inspects ``.content`` and ``.tool_calls``;
    a plain object with those two attributes satisfies the contract
    without spinning up the real LLM client.
    """

    def __init__(
        self,
        *,
        content: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


def _capturing_rag_service() -> MagicMock:
    """Empty-RAG stub — never returns chunks, so the retrieve node
    produces ``rag_messages=[]`` and the LLM node runs in the
    no-RAG path.
    """
    from knowledge.rag_service import RagContext

    svc = MagicMock()
    svc.build_context_for_query = AsyncMock(
        return_value=RagContext(
            system_message="",
            chunk_count=0,
            knowledge_base_id="",
            knowledge_base_name="",
            retrieval_score_max=0.0,
        )
    )
    return svc


def _llm_client_factory_returning(
    client: MagicMock,
) -> Callable[[str], Awaitable[Any]]:
    """Build an async per-tenant LLM factory that always returns ``client``.

    The factory is ``async`` so it matches the production shape
    (M4.C cached resolver). Tests that want to assert ``chat`` was
    awaited pass a mock with ``AsyncMock`` methods.
    """

    async def _factory(_tenant_id: str) -> Any:
        return client

    return _factory


def _make_state(*, tenant_id: str, conversation_id: str, query: str) -> AgentState:
    """Build a minimal agent state with one customer turn."""
    return AgentState(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        messages=[HumanMessage(content=query)],
        rag_messages=[],
        final_text=None,
        escalated=False,
        escalation_message=None,
        tool_iterations=0,
        on_delta=None,
    )


# ---------------------------------------------------------------------------
# Step 4.1 — LangGraph node spans
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_each_node_emits_span(memory_exporter: Any) -> None:
    """A normal agent run emits one ``agent.node.<name>`` span per node.

    For the no-RAG / no-tool path the linear run touches the
    ``retrieve`` + ``llm`` nodes and skips ``escalation``; we
    assert at least both core nodes fired.
    """
    client = MagicMock()
    client.chat = AsyncMock(return_value=_StubChatResponse(content="hi there"))
    client.stream_chat = MagicMock()  # unused — graph takes the non-stream path

    graph = build_agent_graph(
        rag_service=_capturing_rag_service(),
        llm_client_factory=_llm_client_factory_returning(client),
        model=DEFAULT_MODEL,
    )

    state = _make_state(
        tenant_id="t-otel-m6-4",
        conversation_id="c-otel-m6-4",
        query="hello",
    )
    await graph.ainvoke(dict(state))

    spans = memory_exporter.get_finished_spans()
    names = sorted({s.name for s in spans})
    node_spans = [s.name for s in spans if s.name.startswith("agent.node.")]
    # At minimum we expect the retrieve + llm nodes to have fired;
    # escalation is only used when the LLM calls escalate_to_human.
    assert "agent.node.retrieve" in node_spans
    assert "agent.node.llm" in node_spans


@pytest.mark.integration
@pytest.mark.asyncio
async def test_node_spans_share_trace_id(memory_exporter: Any) -> None:
    """All ``agent.node.*`` spans in a single turn share one trace_id.

    The OTel ``trace_id`` is the same for every span inside one
    logical operation; the parent-child relationship is what
    distinguishes nodes from each other, not separate traces.

    We open a wrapping span around ``graph.ainvoke(...)`` to model
    the production wire — in production the call sits under a
    ``channel.inbound`` / FastAPI request span, so the node
    spans nest under a single trace. Without an explicit parent,
    each ``tracer.start_as_current_span`` opens its own trace.
    """
    from observability import tracer

    client = MagicMock()
    client.chat = AsyncMock(return_value=_StubChatResponse(content="hi there"))
    graph = build_agent_graph(
        rag_service=_capturing_rag_service(),
        llm_client_factory=_llm_client_factory_returning(client),
        model=DEFAULT_MODEL,
    )

    state = _make_state(
        tenant_id="t-otel-trace",
        conversation_id="c-otel-trace",
        query="hello",
    )
    with tracer.start_as_current_span("agent.turn.test_wrapper"):
        await graph.ainvoke(dict(state))

    spans = memory_exporter.get_finished_spans()
    node_spans = [s for s in spans if s.name.startswith("agent.node.")]
    assert len(node_spans) >= 2
    trace_ids = {s.context.trace_id for s in node_spans}
    assert len(trace_ids) == 1, (
        f"expected a single trace_id, got {len(trace_ids)}: "
        f"{[hex(t) for t in trace_ids]}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_node_span_carries_whitelisted_attributes(
    memory_exporter: Any,
) -> None:
    """Each node span carries ``tenant_id`` + ``conversation.id``.

    These are the only PII-whitelisted attributes attached to
    the node spans; messages / queries / chunk text are not
    attached.
    """
    client = MagicMock()
    client.chat = AsyncMock(return_value=_StubChatResponse(content="hi there"))
    graph = build_agent_graph(
        rag_service=_capturing_rag_service(),
        llm_client_factory=_llm_client_factory_returning(client),
        model=DEFAULT_MODEL,
    )

    state = _make_state(
        tenant_id="t-otel-attrs",
        conversation_id="c-otel-attrs",
        query="hello",
    )
    await graph.ainvoke(dict(state))

    spans = memory_exporter.get_finished_spans()
    retrieve_spans = [s for s in spans if s.name == "agent.node.retrieve"]
    llm_spans = [s for s in spans if s.name == "agent.node.llm"]
    assert len(retrieve_spans) == 1
    assert len(llm_spans) == 1

    retrieve_attrs = dict(retrieve_spans[0].attributes or {})
    llm_attrs = dict(llm_spans[0].attributes or {})
    assert retrieve_attrs.get("tenant_id") == "t-otel-attrs"
    assert retrieve_attrs.get("conversation.id") == "c-otel-attrs"
    assert llm_attrs.get("tenant_id") == "t-otel-attrs"
    assert llm_attrs.get("conversation.id") == "c-otel-attrs"


# ---------------------------------------------------------------------------
# Step 4.2 — Tool-call span child of the LLM node span
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tool_call_emits_child_span(memory_exporter: Any) -> None:
    """A ``search_internal_kb`` tool invocation emits an
    ``agent.tool.search_internal_kb`` child span.

    Pass ``kb_repository`` so :func:`build_agent_graph` hoists the
    ``search_internal_kb`` tool automatically (mirrors production
    wiring via ``SimpleResponder``). The LLM emits one tool_use;
    the dispatcher awaits it inside an ``agent.tool.*`` span.
    """
    from unittest.mock import MagicMock

    search_svc = MagicMock()
    search_svc.retrieve = AsyncMock(return_value=[
        {
            "chunk_id": "c1",
            "article_title": "Test",
            "score": 0.85,
            "content": "snippet",
        }
    ])
    kb_repo = MagicMock()
    kb_repo.find_by_slug = AsyncMock(return_value=None)

    # LLM emits one tool_use for search_internal_kb; the second
    # call returns the text reply (the LLM's response after
    # seeing the tool result).
    tool_call = {
        "type": "tool_use",
        "id": "toolu-test",
        "name": "search_internal_kb",
        "input": {"query": "test"},
    }
    client = MagicMock()
    client.chat = AsyncMock(side_effect=[
        _StubChatResponse(content="", tool_calls=[tool_call]),
        _StubChatResponse(content="final answer", tool_calls=[]),
    ])
    # ``qdrant_client=None`` keeps the multimodal tool from being
    # hoisted (per the graph's two-condition guard).
    graph = build_agent_graph(
        rag_service=_capturing_rag_service(),
        llm_client_factory=_llm_client_factory_returning(client),
        model=DEFAULT_MODEL,
        kb_repository=kb_repo,
        # ``conv_service=None`` — the escalate tool stays out of
        # the advertised tool list so the search tool fires
        # deterministically.
        conv_service=None,
    )

    state = _make_state(
        tenant_id="t-otel-tool",
        conversation_id="c-otel-tool",
        query="search please",
    )
    ctx_token = bind_escalation_context(
        tenant_id="t-otel-tool", conversation_id="c-otel-tool"
    )
    try:
        await graph.ainvoke(dict(state))
    finally:
        reset_escalation_context(ctx_token)

    spans = memory_exporter.get_finished_spans()
    tool_spans = [s.name for s in spans if s.name.startswith("agent.tool.")]
    assert any("search_internal_kb" in n for n in tool_spans), (
        f"expected an agent.tool.search_internal_kb span, "
        f"got tool spans={tool_spans}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tool_span_carries_whitelisted_attributes(
    memory_exporter: Any,
) -> None:
    """Tool-call spans carry ``tenant_id`` + ``conversation.id``.

    Tool args (``query``, ``kb_slug``, ``top_k``) are deliberately
    NOT attached — the LLM-supplied ``query`` may carry customer
    text and is PII-bearing.
    """
    search_svc = MagicMock()
    search_svc.retrieve = AsyncMock(return_value=[
        {
            "chunk_id": "c1",
            "article_title": "Test",
            "score": 0.85,
            "content": "snippet",
        }
    ])
    kb_repo = MagicMock()
    kb_repo.find_by_slug = AsyncMock(return_value=None)

    tool_call = {
        "type": "tool_use",
        "id": "toolu-test",
        "name": "search_internal_kb",
        "input": {"query": "attrs-test"},
    }
    client = MagicMock()
    client.chat = AsyncMock(side_effect=[
        _StubChatResponse(content="", tool_calls=[tool_call]),
        _StubChatResponse(content="final answer", tool_calls=[]),
    ])

    graph = build_agent_graph(
        rag_service=_capturing_rag_service(),
        llm_client_factory=_llm_client_factory_returning(client),
        model=DEFAULT_MODEL,
        kb_repository=kb_repo,
        conv_service=None,
    )

    state = _make_state(
        tenant_id="t-otel-tool-attrs",
        conversation_id="c-otel-tool-attrs",
        query="search please",
    )
    ctx_token = bind_escalation_context(
        tenant_id="t-otel-tool-attrs",
        conversation_id="c-otel-tool-attrs",
    )
    try:
        await graph.ainvoke(dict(state))
    finally:
        reset_escalation_context(ctx_token)

    spans = memory_exporter.get_finished_spans()
    tool_spans = [s for s in spans if s.name.startswith("agent.tool.")]
    assert tool_spans, "expected at least one agent.tool.* span"
    attrs = dict(tool_spans[0].attributes or {})
    assert attrs.get("tenant_id") == "t-otel-tool-attrs"
    assert attrs.get("conversation.id") == "c-otel-tool-attrs"
    # PII discipline — the query arg is NOT attached to the span.
    forbidden = ("query", "kb_slug", "top_k")
    for k in forbidden:
        assert k not in attrs, (
            f"tool args must NOT be attached to span (PII), "
            f"got {k!r} in {attrs!r}"
        )
