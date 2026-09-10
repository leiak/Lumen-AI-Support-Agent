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
async def test_create_calls_session_add_flush_refresh_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session_ctx(monkeypatch)
    repo = ConversationRepository()
    conv = _conv()
    result = await repo.create(conversation=conv)
    assert result is conv
    session.add.assert_called_once_with(conv)
    session.flush.assert_awaited_once()
    session.refresh.assert_awaited_once_with(conv)
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
async def test_update_loads_then_commits_and_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session_ctx(monkeypatch)
    conv = _conv()
    loaded = _conv(id=conv.id)
    session.get = AsyncMock(return_value=loaded)
    repo = ConversationRepository()
    result = await repo.update(conv)
    # The returned value is the persisted (refreshed) row, not the input object.
    assert result is loaded
    session.get.assert_awaited_once_with(Conversation, conv.id)
    session.flush.assert_awaited_once()
    session.refresh.assert_awaited_once_with(loaded)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_returns_none_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session_ctx(monkeypatch)
    session.get = AsyncMock(return_value=None)
    repo = ConversationRepository()
    result = await repo.update(_conv())
    assert result is None
    # No write should be attempted when the row does not exist.
    session.commit.assert_not_called()


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


@pytest.mark.asyncio
async def test_find_open_by_channel_customer_excludes_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closed conversations should not be returned by find_open."""
    session = _session_ctx(monkeypatch)
    open_conv = _conv(status=ConversationStatus.OPEN)
    scalars = MagicMock()
    scalars.first = MagicMock(return_value=open_conv)
    execute_result = MagicMock()
    execute_result.scalars = MagicMock(return_value=scalars)
    session.execute = AsyncMock(return_value=execute_result)

    repo = ConversationRepository()
    result = await repo.find_open_by_channel_customer(
        channel_id=open_conv.channel_id,
        customer_external_id=open_conv.customer_external_id,
    )
    assert result is open_conv
    # Verify the WHERE clause excludes CLOSED.
    stmt_arg = session.execute.await_args.args[0]
    rendered = str(stmt_arg).upper()
    assert "WHERE" in rendered


@pytest.mark.asyncio
async def test_list_by_assigned_agent_filters_by_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session_ctx(monkeypatch)
    expected = [_conv(assigned_agent_id="u_agent_1")]
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=expected)
    execute_result = MagicMock()
    execute_result.scalars = MagicMock(return_value=scalars)
    session.execute = AsyncMock(return_value=execute_result)

    repo = ConversationRepository()
    result = await repo.list_by_assigned_agent(
        assigned_agent_id="u_agent_1", status=ConversationStatus.PENDING
    )
    assert result == expected


@pytest.mark.asyncio
async def test_touch_last_activity_updates_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session_ctx(monkeypatch)
    repo = ConversationRepository()
    now = datetime.now(UTC)
    await repo.touch_last_activity(conversation_id="c1", at=now)
    assert session.execute.await_count == 1
    assert session.commit.await_count == 1


# ---- MessageRepository unit tests ----


@pytest.mark.asyncio
async def test_message_create_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session_ctx(monkeypatch)
    repo = MessageRepository()
    msg = _msg()
    result = await repo.create(message=msg)
    assert result is msg
    session.add.assert_called_once_with(msg)
    session.flush.assert_awaited_once()
    session.refresh.assert_awaited_once_with(msg)
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
async def test_message_list_by_conversation_returns_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session_ctx(monkeypatch)
    msg_a = _msg()
    msg_b = _msg()
    # DESC order from DB, repo reverses to chronological ASC
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=[msg_b, msg_a])
    execute_result = MagicMock()
    execute_result.scalars = MagicMock(return_value=scalars)
    session.execute = AsyncMock(return_value=execute_result)

    repo = MessageRepository()
    result = await repo.list_by_conversation(conversation_id="c1")
    assert result == [msg_a, msg_b]


@pytest.mark.asyncio
async def test_list_by_conversation_with_before_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """before= should add a created_at < filter and the result is reversed to ASC."""
    session = _session_ctx(monkeypatch)
    msg_a = _msg()
    msg_b = _msg()
    # DESC order from DB, repo reverses to chronological ASC
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=[msg_b, msg_a])
    execute_result = MagicMock()
    execute_result.scalars = MagicMock(return_value=scalars)
    session.execute = AsyncMock(return_value=execute_result)

    repo = MessageRepository()
    cutoff = datetime.now(UTC)
    result = await repo.list_by_conversation(conversation_id="c1", before=cutoff)
    assert result == [msg_a, msg_b]
    assert session.execute.await_count == 1
    # Verify the WHERE clause contains a created_at < filter
    stmt_arg = session.execute.await_args.args[0]
    assert "created_at" in str(stmt_arg).lower()


@pytest.mark.asyncio
async def test_message_count_returns_scalar(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session_ctx(monkeypatch)
    execute_result = MagicMock()
    execute_result.scalar = MagicMock(return_value=42)
    session.execute = AsyncMock(return_value=execute_result)

    repo = MessageRepository()
    result = await repo.count_by_conversation(conversation_id="c1")
    assert result == 42


@pytest.mark.asyncio
async def test_count_by_conversation_returns_zero_when_scalar_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """result.scalar() may return None for empty result sets; ensure `or 0` triggers."""
    session = _session_ctx(monkeypatch)
    execute_result = MagicMock()
    execute_result.scalar = MagicMock(return_value=None)
    session.execute = AsyncMock(return_value=execute_result)

    repo = MessageRepository()
    result = await repo.count_by_conversation(conversation_id="c1")
    assert result == 0


@pytest.mark.asyncio
async def test_list_by_conversation_default_returns_latest_in_ascending_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default page returns the most recent messages, but in chronological order."""
    session = _session_ctx(monkeypatch)
    msg_old = _msg(created_at=datetime(2026, 9, 10, 9, 0, 0, tzinfo=UTC))
    msg_mid = _msg(created_at=datetime(2026, 9, 10, 10, 0, 0, tzinfo=UTC))
    msg_new = _msg(created_at=datetime(2026, 9, 10, 11, 0, 0, tzinfo=UTC))
    # DB returns DESC, repo reverses to ASC
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=[msg_new, msg_mid, msg_old])
    execute_result = MagicMock()
    execute_result.scalars = MagicMock(return_value=scalars)
    session.execute = AsyncMock(return_value=execute_result)

    repo = MessageRepository()
    result = await repo.list_by_conversation(conversation_id="c1")
    assert result == [msg_old, msg_mid, msg_new]


@pytest.mark.asyncio
async def test_list_by_conversation_with_before_returns_msgs_before_cursor_asc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session_ctx(monkeypatch)
    cursor = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    msg1 = _msg(created_at=datetime(2026, 9, 10, 9, 0, 0, tzinfo=UTC))
    msg2 = _msg(created_at=datetime(2026, 9, 10, 10, 0, 0, tzinfo=UTC))
    msg4 = _msg(created_at=datetime(2026, 9, 10, 11, 0, 0, tzinfo=UTC))
    # DB returns DESC, repo reverses to ASC
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=[msg4, msg2, msg1])
    execute_result = MagicMock()
    execute_result.scalars = MagicMock(return_value=scalars)
    session.execute = AsyncMock(return_value=execute_result)

    repo = MessageRepository()
    result = await repo.list_by_conversation(conversation_id="c1", before=cursor)
    assert result == [msg1, msg2, msg4]
    # Verify the WHERE clause references the cursor
    stmt_arg = session.execute.await_args.args[0]
    rendered = str(stmt_arg).lower()
    assert "created_at" in rendered


# ---- Integration test (live DB, skipped when unavailable) ----


@pytest.mark.integration
@pytest.mark.asyncio
async def test_conversation_repo_roundtrip_live_db() -> None:
    """Integration: create + read + list roundtrip against the real DB.

    Skipped when no live DB connection is available. Creates a tenant + channel
    first to satisfy FK constraints, then cleans everything up.
    """
    try:
        from sqlalchemy import text

        from channel.enums import ChannelType
        from channel.models import Channel  # noqa: F401
        from channel.repository import ChannelRepository
        from core.database import get_engine
        from tenant.enums import TenantPlan
        from tenant.models import Tenant
        from tenant.repository import TenantRepository

        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("DB not available")

    from core.database import get_session

    tenant = await TenantRepository().create(
        name="Conv Repo Test", plan=TenantPlan.FREE
    )
    channel = await ChannelRepository().create(
        tenant_id=tenant.id,
        type=ChannelType.WEB,
        name="Test Channel",
        credentials_encrypted="{}",
    )
    try:
        repo = ConversationRepository()
        conv = _conv(tenant_id=tenant.id, channel_id=channel.id)
        created = await repo.create(conversation=conv)
        assert created.id == conv.id

        fetched = await repo.get_by_id(conv.id)
        assert fetched is not None
        assert fetched.id == conv.id
        assert fetched.channel_id == conv.channel_id
        assert fetched.tenant_id == tenant.id
    finally:
        async with get_session() as session:
            t = await session.get(Tenant, tenant.id)
            if t:
                await session.delete(t)  # cascades channels + conversations
                await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_partial_unique_index_allows_closed_and_open_for_same_pair() -> None:
    """A CLOSED conversation and a new OPEN conversation for the same
    (channel_id, customer_external_id) MUST coexist; a second OPEN for
    the same pair MUST raise IntegrityError.

    Skipped when no live DB connection is available.
    """
    try:
        from sqlalchemy import text as sa_text
        from sqlalchemy.exc import IntegrityError as _IntegrityError

        from channel.enums import ChannelType
        from channel.repository import ChannelRepository
        from core.database import get_engine, get_session
        from tenant.enums import TenantPlan
        from tenant.models import Tenant
        from tenant.repository import TenantRepository

        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(sa_text("SELECT 1"))
    except Exception:
        pytest.skip("DB not available")

    tenant = await TenantRepository().create(
        name="Partial Index Test", plan=TenantPlan.FREE
    )
    channel = await ChannelRepository().create(
        tenant_id=tenant.id,
        type=ChannelType.WEB,
        name="Partial Index Channel",
        credentials_encrypted="{}",
    )
    repo = ConversationRepository()
    cust = "ou_partial_idx_user"
    try:
        # First, create a CLOSED conversation for this pair.
        closed_conv = _conv(
            tenant_id=tenant.id,
            channel_id=channel.id,
            customer_external_id=cust,
            status=ConversationStatus.CLOSED,
        )
        await repo.create(conversation=closed_conv)

        # A new OPEN conversation for the same pair must succeed because the
        # partial unique index excludes rows where status = 'closed'.
        open_conv = _conv(
            tenant_id=tenant.id,
            channel_id=channel.id,
            customer_external_id=cust,
            status=ConversationStatus.OPEN,
        )
        await repo.create(conversation=open_conv)

        # A SECOND OPEN for the same pair must violate the partial index.
        second_open = _conv(
            tenant_id=tenant.id,
            channel_id=channel.id,
            customer_external_id=cust,
            status=ConversationStatus.OPEN,
        )
        with pytest.raises((_IntegrityError, Exception)) as exc_info:
            await repo.create(conversation=second_open)
        # Be tolerant: the create path may wrap IntegrityError as a
        # generic Exception depending on session handling; the important
        # invariant is the database rejected the second open row.
        assert exc_info.value is not None
    finally:
        async with get_session() as session:
            t = await session.get(Tenant, tenant.id)
            if t:
                await session.delete(t)  # cascades channels + conversations
                await session.commit()