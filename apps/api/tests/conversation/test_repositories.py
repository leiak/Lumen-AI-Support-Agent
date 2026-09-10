"""Tests for Conversation and Message repositories.

These are unit tests that mock `core.database.get_session` so the repository
code paths can be exercised without a live database. Each test patches
`conversation.repository.get_session` to yield a MagicMock session, then
asserts against the recorded calls.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation, Message
from conversation.repository import ConversationRepository, MessageRepository
from core.id_gen import new_id


def _conv(**kwargs) -> Conversation:
    base = dict(
        id=new_id(),
        tenant_id=new_id(),
        channel_id=new_id(),
        customer_external_id="ou_user_1",
        status=ConversationStatus.OPEN,
        ai_handling=True,
        opened_at=datetime.now(UTC),
        last_activity_at=datetime.now(UTC),
        assigned_agent_id=None,
    )
    base.update(kwargs)
    return Conversation(**base)


def _msg(**kwargs) -> Message:
    base = dict(
        id=new_id(),
        conversation_id=new_id(),
        role=MessageRole.CUSTOMER,
        content_text="hi",
        sender_id=None,
        created_at=datetime.now(UTC),
        content_blocks_json=None,
        tool_calls_json=None,
    )
    base.update(kwargs)
    return Message(**base)


def _session_ctx(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Patch conversation.repository.get_session so it yields a MagicMock session.

    Returns the mock session so tests can assert against its calls.
    """
    mock_session = MagicMock()
    # scalar()/scalars() helpers are regular sync methods on the result.
    mock_session.execute = AsyncMock()
    mock_session.get = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.refresh = AsyncMock()
    mock_session.rollback = AsyncMock()

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_session)
    cm.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr("conversation.repository.get_session", lambda: cm)
    return mock_session


# ---- ConversationRepository unit tests ----


@pytest.mark.asyncio
async def test_create_calls_session_add_and_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session_ctx(monkeypatch)
    repo = ConversationRepository()
    conv = _conv()
    result = await repo.create(conversation=conv)
    assert result is conv
    session.add.assert_called_once_with(conv)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_by_id_returns_session_get_result(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session_ctx(monkeypatch)
    conv = _conv()
    session.get = AsyncMock(return_value=conv)
    repo = ConversationRepository()
    result = await repo.get_by_id(conv.id)
    assert result is conv
    session.get.assert_awaited_once_with(Conversation, conv.id)


@pytest.mark.asyncio
async def test_get_by_id_returns_none_for_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session_ctx(monkeypatch)
    session.get = AsyncMock(return_value=None)
    repo = ConversationRepository()
    result = await repo.get_by_id("01HX_MISSING")
    assert result is None


@pytest.mark.asyncio
async def test_update_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session_ctx(monkeypatch)
    conv = _conv()
    repo = ConversationRepository()
    result = await repo.update(conv)
    assert result is conv
    session.add.assert_called_once_with(conv)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_by_tenant_filters_by_status(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session_ctx(monkeypatch)
    expected = [_conv(status=ConversationStatus.OPEN)]
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=expected)
    execute_result = MagicMock()
    execute_result.scalars = MagicMock(return_value=scalars)
    session.execute = AsyncMock(return_value=execute_result)

    repo = ConversationRepository()
    result = await repo.list_by_tenant(tenant_id="t1", status=ConversationStatus.OPEN)
    assert result == expected
    session.execute.assert_awaited_once()


# ---- MessageRepository unit tests ----


@pytest.mark.asyncio
async def test_message_create_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session_ctx(monkeypatch)
    repo = MessageRepository()
    msg = _msg()
    result = await repo.create(message=msg)
    assert result is msg
    session.add.assert_called_once_with(msg)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_message_get_by_id_returns_result(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session_ctx(monkeypatch)
    msg = _msg()
    session.get = AsyncMock(return_value=msg)
    repo = MessageRepository()
    result = await repo.get_by_id(msg.id)
    assert result is msg


@pytest.mark.asyncio
async def test_message_list_by_conversation_returns_list(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session_ctx(monkeypatch)
    expected = [_msg(), _msg()]
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=expected)
    execute_result = MagicMock()
    execute_result.scalars = MagicMock(return_value=scalars)
    session.execute = AsyncMock(return_value=execute_result)

    repo = MessageRepository()
    result = await repo.list_by_conversation(conversation_id="c1")
    assert result == expected


@pytest.mark.asyncio
async def test_message_count_returns_scalar(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session_ctx(monkeypatch)
    execute_result = MagicMock()
    execute_result.scalar = MagicMock(return_value=42)
    session.execute = AsyncMock(return_value=execute_result)

    repo = MessageRepository()
    result = await repo.count_by_conversation(conversation_id="c1")
    assert result == 42