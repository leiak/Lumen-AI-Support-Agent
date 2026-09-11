"""Tests for the M1 simple AI responder."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.simple_responder import (
    DEFAULT_MODEL,
    FALLBACK_MESSAGE,
    MAX_HISTORY_MESSAGES,
    AgentResponse,
    SimpleResponder,
)
from conversation.enums import ConversationStatus, MessageRole
from knowledge.rag_service import RagContext
from llm_client.types import ChatResponse


def _empty_rag_context() -> RagContext:
    """A no-op RAG context for unit tests that don't exercise RAG."""
    return RagContext(
        system_message="",
        chunk_count=0,
        knowledge_base_id="",
        knowledge_base_name="",
        retrieval_score_max=0.0,
    )


def _empty_rag_service() -> MagicMock:
    """A MagicMock for ``RAGService`` whose ``build_context_for_query``
    returns an empty :class:`RagContext`.

    The simple-responder unit tests don't seed a tenant / KB / Qdrant,
    so any RAG call would otherwise try to query a real (or absent)
    database. The mock preserves the no-RAG behavior these tests
    were originally written against.
    """
    svc = MagicMock()
    svc.build_context_for_query = AsyncMock(return_value=_empty_rag_context())
    return svc


def _conv(**overrides: object) -> MagicMock:
    base = dict(
        id="c1",
        tenant_id="t1",
        channel_id="ch1",
        customer_external_id="u1",
        status=ConversationStatus.OPEN,
        ai_handling=True,
        assigned_agent_id=None,
        opened_at=None,
        last_activity_at=None,
    )
    base.update(overrides)
    return MagicMock(**base)


def _msg(role: MessageRole, text: str) -> MagicMock:
    m = MagicMock()
    m.role = role
    m.content_text = text
    return m


def _fake_client(content: str = "ok") -> MagicMock:
    client = MagicMock()
    client.chat = AsyncMock(
        return_value=ChatResponse(
            content=content,
            model=DEFAULT_MODEL,
            prompt_tokens=10,
            completion_tokens=5,
            finish_reason="stop",
        )
    )
    return client


@pytest.mark.asyncio
async def test_responder_returns_none_when_conversation_not_found() -> None:
    conv_service = MagicMock()
    conv_service.get = AsyncMock(return_value=None)
    responder = SimpleResponder(
        conv_service=conv_service,
        llm_client_factory=lambda t: AsyncMock(),
        rag_service=_empty_rag_service(),
    )

    result = await responder.respond(tenant_id="t1", conversation_id="c1")

    assert result is None


@pytest.mark.asyncio
async def test_responder_skips_when_not_ai_handling() -> None:
    """If conversation was transferred to a human, AI should not respond."""
    conv = _conv(ai_handling=False, status=ConversationStatus.PENDING)
    conv_service = MagicMock()
    conv_service.get = AsyncMock(return_value=conv)
    responder = SimpleResponder(
        conv_service=conv_service,
        llm_client_factory=lambda t: AsyncMock(),
        rag_service=_empty_rag_service(),
    )

    result = await responder.respond(tenant_id="t1", conversation_id="c1")

    assert result is None
    conv_service.list_messages.assert_not_called()


@pytest.mark.asyncio
async def test_responder_returns_ai_response_on_llm_success() -> None:
    conv = _conv(ai_handling=True)
    conv_service = MagicMock()
    conv_service.get = AsyncMock(return_value=conv)
    conv_service.list_messages = AsyncMock(
        return_value=[_msg(MessageRole.CUSTOMER, "How do I reset my password?")]
    )

    fake_client = _fake_client(
        content="Click 'Forgot password' on the login page."
    )

    responder = SimpleResponder(
        conv_service=conv_service,
        llm_client_factory=lambda t: fake_client,
        rag_service=_empty_rag_service(),
    )

    result = await responder.respond(tenant_id="t1", conversation_id="c1")

    assert result is not None
    assert isinstance(result, AgentResponse)
    assert result.role == MessageRole.AI
    assert "password" in result.content_text.lower()

    # Verify chat request was constructed correctly
    call_args = fake_client.chat.await_args.args[0]
    assert call_args.model == DEFAULT_MODEL
    assert len(call_args.messages) == 2  # system + 1 customer
    assert call_args.messages[0].role == "system"


@pytest.mark.asyncio
async def test_responder_returns_fallback_on_llm_failure() -> None:
    conv = _conv(ai_handling=True)
    conv_service = MagicMock()
    conv_service.get = AsyncMock(return_value=conv)
    conv_service.list_messages = AsyncMock(return_value=[])

    fake_client = MagicMock()
    fake_client.chat = AsyncMock(side_effect=RuntimeError("api down"))

    responder = SimpleResponder(
        conv_service=conv_service,
        llm_client_factory=lambda t: fake_client,
        rag_service=_empty_rag_service(),
    )

    result = await responder.respond(tenant_id="t1", conversation_id="c1")

    assert result is not None
    assert result.role == MessageRole.AI
    assert result.content_text == FALLBACK_MESSAGE


@pytest.mark.asyncio
async def test_responder_returns_fallback_on_empty_llm_content() -> None:
    """LLM returning empty content triggers the same fallback as a hard failure."""
    conv = _conv(ai_handling=True)
    conv_service = MagicMock()
    conv_service.get = AsyncMock(return_value=conv)
    conv_service.list_messages = AsyncMock(return_value=[])

    fake_client = MagicMock()
    fake_client.chat = AsyncMock(return_value=ChatResponse(
        content="",  # empty
        model="claude-haiku-4-5",
        prompt_tokens=10, completion_tokens=0,
        finish_reason="stop",
    ))

    responder = SimpleResponder(
        conv_service=conv_service,
        llm_client_factory=lambda t: fake_client,
        rag_service=_empty_rag_service(),
    )

    result = await responder.respond(tenant_id="t1", conversation_id="c1")
    assert result is not None
    assert result.role == MessageRole.AI
    assert result.content_text == FALLBACK_MESSAGE


@pytest.mark.asyncio
async def test_responder_keeps_latest_messages_when_overflowed() -> None:
    """When the repo returns > MAX_HISTORY_MESSAGES, the LATEST are kept."""
    conv = _conv(ai_handling=True)
    conv_service = MagicMock()
    conv_service.get = AsyncMock(return_value=conv)
    # 25 messages — older first, newer last (ascending chronological order)
    many = [
        _msg(MessageRole.CUSTOMER if i % 2 == 0 else MessageRole.AI, f"msg {i}")
        for i in range(25)
    ]
    conv_service.list_messages = AsyncMock(return_value=many)

    fake_client = MagicMock()
    fake_client.chat = AsyncMock(return_value=ChatResponse(
        content="ok", model="claude-haiku-4-5",
        prompt_tokens=10, completion_tokens=5, finish_reason="stop",
    ))

    responder = SimpleResponder(
        conv_service=conv_service,
        llm_client_factory=lambda t: fake_client,
        rag_service=_empty_rag_service(),
    )
    await responder.respond(tenant_id="t1", conversation_id="c1")

    chat_messages = fake_client.chat.await_args.args[0].messages
    # Drop the system message, then assert we have the LATEST 20 (msg 5..24)
    non_system = [m for m in chat_messages if m.role != "system"]
    assert len(non_system) == 20
    # The first user/assistant message in the request should be from msg 5
    first_text = non_system[0].content
    assert "msg 5" in first_text
    # The last should be from msg 24
    last_text = non_system[-1].content
    assert "msg 24" in last_text


@pytest.mark.asyncio
async def test_responder_skips_tool_messages_in_history() -> None:
    """TOOL-role messages are not consumed by the simple responder."""
    conv = _conv(ai_handling=True)
    conv_service = MagicMock()
    conv_service.get = AsyncMock(return_value=conv)
    conv_service.list_messages = AsyncMock(
        return_value=[
            _msg(MessageRole.CUSTOMER, "hi"),
            _msg(MessageRole.TOOL, '{"result": "foo"}'),
            _msg(MessageRole.AI, "hello"),
        ]
    )

    fake_client = _fake_client(content="ok")

    responder = SimpleResponder(
        conv_service=conv_service,
        llm_client_factory=lambda t: fake_client,
        rag_service=_empty_rag_service(),
    )
    await responder.respond(tenant_id="t1", conversation_id="c1")

    chat_messages = fake_client.chat.await_args.args[0].messages
    roles = [m.role for m in chat_messages]
    assert "tool" not in roles
    assert roles == ["system", "user", "assistant"]


@pytest.mark.asyncio
async def test_responder_caps_history_at_max_history_messages() -> None:
    conv = _conv(ai_handling=True)
    conv_service = MagicMock()
    conv_service.get = AsyncMock(return_value=conv)
    # 30 customer messages — more than the cap.
    many = [_msg(MessageRole.CUSTOMER, f"msg {i}") for i in range(30)]
    conv_service.list_messages = AsyncMock(return_value=many)

    fake_client = _fake_client(content="ok")

    responder = SimpleResponder(
        conv_service=conv_service,
        llm_client_factory=lambda t: fake_client,
        rag_service=_empty_rag_service(),
    )
    await responder.respond(tenant_id="t1", conversation_id="c1")

    call_args = fake_client.chat.await_args.args[0]
    # Should be MAX_HISTORY_MESSAGES + 1 system message.
    assert len(call_args.messages) == MAX_HISTORY_MESSAGES + 1


@pytest.mark.asyncio
async def test_responder_maps_agent_and_ai_to_assistant_role() -> None:
    """Both AGENT (human) and AI past messages become 'assistant' turns."""
    conv = _conv(ai_handling=True)
    conv_service = MagicMock()
    conv_service.get = AsyncMock(return_value=conv)
    conv_service.list_messages = AsyncMock(
        return_value=[
            _msg(MessageRole.CUSTOMER, "c1"),
            _msg(MessageRole.AI, "a1"),
            _msg(MessageRole.AGENT, "a2"),
            _msg(MessageRole.SYSTEM, "sys"),
            _msg(MessageRole.CUSTOMER, "c2"),
        ]
    )

    fake_client = _fake_client(content="ok")

    responder = SimpleResponder(
        conv_service=conv_service,
        llm_client_factory=lambda t: fake_client,
        rag_service=_empty_rag_service(),
    )
    await responder.respond(tenant_id="t1", conversation_id="c1")

    chat_messages = fake_client.chat.await_args.args[0].messages
    # First is the system prompt we built, rest follow in order.
    assert chat_messages[0].role == "system"
    assert chat_messages[1].role == "user" and chat_messages[1].content == "c1"
    assert chat_messages[2].role == "assistant" and chat_messages[2].content == "a1"
    assert chat_messages[3].role == "assistant" and chat_messages[3].content == "a2"
    assert chat_messages[4].role == "system" and chat_messages[4].content == "sys"
    assert chat_messages[5].role == "user" and chat_messages[5].content == "c2"


@pytest.mark.asyncio
async def test_responder_handles_list_messages_returning_none() -> None:
    """If list_messages returns None (cross-tenant or missing conv),
    responder should treat it as an empty history, not crash.
    """
    conv = _conv(ai_handling=True)
    conv_service = MagicMock()
    conv_service.get = AsyncMock(return_value=conv)
    conv_service.list_messages = AsyncMock(return_value=None)

    fake_client = _fake_client(content="hello")

    responder = SimpleResponder(
        conv_service=conv_service,
        llm_client_factory=lambda t: fake_client,
        rag_service=_empty_rag_service(),
    )

    result = await responder.respond(tenant_id="t1", conversation_id="c1")

    assert result is not None
    assert result.content_text == "hello"
    # Only the system message is sent.
    assert len(fake_client.chat.await_args.args[0].messages) == 1


@pytest.mark.asyncio
async def test_responder_summarizes_overflow_messages() -> None:
    """When conversation has > MAX_HISTORY_BEFORE_SUMMARY messages, oldest are summarized."""
    from agent.simple_responder import MAX_HISTORY_BEFORE_SUMMARY

    conv = _conv(ai_handling=True)
    conv_service = MagicMock()
    conv_service.get = AsyncMock(return_value=conv)
    # 60 messages — well above MAX_HISTORY_BEFORE_SUMMARY (50)
    many = [
        _msg(MessageRole.CUSTOMER if i % 2 == 0 else MessageRole.AI, f"msg {i}")
        for i in range(MAX_HISTORY_BEFORE_SUMMARY + 10)
    ]
    conv_service.list_messages = AsyncMock(return_value=many)

    fake_client = MagicMock()
    # First call (summary) returns a summary, second call (real chat) returns ok
    fake_client.chat = AsyncMock(side_effect=[
        ChatResponse(
            content="customer asked about password reset, agent provided instructions",
            model=DEFAULT_MODEL,
            prompt_tokens=200,
            completion_tokens=30,
            finish_reason="stop",
        ),
        ChatResponse(
            content="ok",
            model=DEFAULT_MODEL,
            prompt_tokens=10,
            completion_tokens=5,
            finish_reason="stop",
        ),
    ])

    responder = SimpleResponder(
        conv_service=conv_service,
        llm_client_factory=lambda t: fake_client,
        rag_service=_empty_rag_service(),
    )
    await responder.respond(tenant_id="t1", conversation_id="c1")

    # The LLM should have been called TWICE: once for summary, once for the real response
    assert fake_client.chat.await_count == 2

    # The second call (the real chat) should have a system message with the summary.
    # respond() prepends M1_SYSTEM_PROMPT, then the summary from _build_history,
    # then the kept messages.
    real_call_args = fake_client.chat.await_args_list[1].args[0]
    summary_msg = real_call_args.messages[1]
    assert summary_msg.role == "system"
    assert "password reset" in summary_msg.content


@pytest.mark.asyncio
async def test_responder_does_not_summarize_below_threshold() -> None:
    """When conversation is <= MAX_HISTORY_BEFORE_SUMMARY, no summary call."""
    conv = _conv(ai_handling=True)
    conv_service = MagicMock()
    conv_service.get = AsyncMock(return_value=conv)
    # 30 messages — below MAX_HISTORY_BEFORE_SUMMARY (50)
    many = [_msg(MessageRole.CUSTOMER, f"msg {i}") for i in range(30)]
    conv_service.list_messages = AsyncMock(return_value=many)

    fake_client = MagicMock()
    fake_client.chat = AsyncMock(return_value=ChatResponse(
        content="ok", model=DEFAULT_MODEL,
        prompt_tokens=10, completion_tokens=5, finish_reason="stop",
    ))

    responder = SimpleResponder(
        conv_service=conv_service,
        llm_client_factory=lambda t: fake_client,
        rag_service=_empty_rag_service(),
    )
    await responder.respond(tenant_id="t1", conversation_id="c1")

    # LLM called only ONCE (the real chat, no summary)
    assert fake_client.chat.await_count == 1


@pytest.mark.asyncio
async def test_responder_summary_failure_uses_truncated_transcript() -> None:
    """If summary LLM call fails, fallback is a truncated transcript."""
    from agent.simple_responder import MAX_HISTORY_BEFORE_SUMMARY

    conv = _conv(ai_handling=True)
    conv_service = MagicMock()
    conv_service.get = AsyncMock(return_value=conv)
    many = [
        _msg(MessageRole.CUSTOMER if i % 2 == 0 else MessageRole.AI, f"msg {i}")
        for i in range(MAX_HISTORY_BEFORE_SUMMARY + 10)
    ]
    conv_service.list_messages = AsyncMock(return_value=many)

    fake_client = MagicMock()
    # First call (summary) fails, second call (real chat) succeeds
    fake_client.chat = AsyncMock(side_effect=[
        RuntimeError("summary failed"),
        ChatResponse(
            content="ok", model=DEFAULT_MODEL,
            prompt_tokens=10, completion_tokens=5, finish_reason="stop",
        ),
    ])

    responder = SimpleResponder(
        conv_service=conv_service,
        llm_client_factory=lambda t: fake_client,
        rag_service=_empty_rag_service(),
    )
    await responder.respond(tenant_id="t1", conversation_id="c1")

    # Real chat still got a system message with a truncated transcript.
    # respond() prepends M1_SYSTEM_PROMPT, then the summary from _build_history.
    real_call_args = fake_client.chat.await_args_list[1].args[0]
    summary_msg = real_call_args.messages[1]
    assert summary_msg.role == "system"
    assert "msg 0" in summary_msg.content  # transcript includes oldest messages
    assert (
        "Earlier conversation" in summary_msg.content
        or "summary" in summary_msg.content.lower()
    )


@pytest.mark.asyncio
async def test_responder_summary_keeps_latest_unchanged() -> None:
    """When summarizing, the latest MAX_HISTORY_MESSAGES are passed as-is (not summarized)."""
    from agent.simple_responder import (
        MAX_HISTORY_BEFORE_SUMMARY,
        MAX_HISTORY_MESSAGES,
    )

    conv = _conv(ai_handling=True)
    conv_service = MagicMock()
    conv_service.get = AsyncMock(return_value=conv)
    # 60 messages with distinctive content for indices 40-59
    msgs = []
    for i in range(MAX_HISTORY_BEFORE_SUMMARY + 10):
        msgs.append(
            _msg(MessageRole.CUSTOMER if i % 2 == 0 else MessageRole.AI, f"msg {i}")
        )
    conv_service.list_messages = AsyncMock(return_value=msgs)

    fake_client = MagicMock()
    fake_client.chat = AsyncMock(side_effect=[
        ChatResponse(
            content="summary text", model=DEFAULT_MODEL,
            prompt_tokens=10, completion_tokens=5, finish_reason="stop",
        ),
        ChatResponse(
            content="ok", model=DEFAULT_MODEL,
            prompt_tokens=10, completion_tokens=5, finish_reason="stop",
        ),
    ])

    responder = SimpleResponder(
        conv_service=conv_service,
        llm_client_factory=lambda t: fake_client,
        rag_service=_empty_rag_service(),
    )
    await responder.respond(tenant_id="t1", conversation_id="c1")

    real_call_args = fake_client.chat.await_args_list[1].args[0]
    # After the summary system msg, the next MAX_HISTORY_MESSAGES messages are the latest 20
    non_system = [m for m in real_call_args.messages if m.role != "system"]
    assert len(non_system) == MAX_HISTORY_MESSAGES
    assert "msg 59" in non_system[-1].content
    assert "msg 40" in non_system[0].content