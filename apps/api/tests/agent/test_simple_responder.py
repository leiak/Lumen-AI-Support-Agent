"""Tests for the M1 simple AI responder."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.simple_responder import (
    DEFAULT_MODEL,
    MAX_HISTORY_MESSAGES,
    AgentResponse,
    SimpleResponder,
)
from conversation.enums import ConversationStatus, MessageRole
from llm_client.types import ChatResponse


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
    )

    result = await responder.respond(tenant_id="t1", conversation_id="c1")

    assert result is not None
    assert result.role == MessageRole.AI
    # Fallback message — either Chinese hint or English-style "AI" works.
    assert "AI" in result.content_text or "人工" in result.content_text


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
    )

    result = await responder.respond(tenant_id="t1", conversation_id="c1")

    assert result is not None
    assert result.content_text == "hello"
    # Only the system message is sent.
    assert len(fake_client.chat.await_args.args[0].messages) == 1