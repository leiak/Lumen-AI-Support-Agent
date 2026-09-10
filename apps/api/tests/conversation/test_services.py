"""Unit tests for ConversationService.

These tests mock the repositories with MagicMock/AsyncMock so the service
logic (tenant scoping, state transitions, side-effects) can be exercised
without a live database. Time is frozen via an injected clock.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation, Message
from conversation.service import ConversationService
from core.id_gen import new_id

FROZEN_NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def _conv(**kwargs: Any) -> Conversation:
    base: dict[str, Any] = dict(
        id=new_id(),
        tenant_id=new_id(),
        channel_id=new_id(),
        customer_external_id="ou_user_1",
        status=ConversationStatus.OPEN,
        ai_handling=True,
        opened_at=FROZEN_NOW,
        last_activity_at=FROZEN_NOW,
        assigned_agent_id=None,
    )
    base.update(kwargs)
    return Conversation(**base)


def _msg(**kwargs: Any) -> Message:
    base: dict[str, Any] = dict(
        id=new_id(),
        conversation_id=new_id(),
        role=MessageRole.CUSTOMER,
        content_text="hi",
        sender_id=None,
        created_at=FROZEN_NOW,
        content_blocks_json=None,
        tool_calls_json=None,
    )
    base.update(kwargs)
    return Message(**base)


def _service_with_repos(
    *,
    conversation_repo: Any = None,
    message_repo: Any = None,
) -> tuple[ConversationService, Any, Any]:
    conv_repo = conversation_repo or MagicMock()
    msg_repo = message_repo or MagicMock()
    svc = ConversationService(
        repo=conv_repo,
        message_repo=msg_repo,
        clock=lambda: FROZEN_NOW,
    )
    return svc, conv_repo, msg_repo


# ---- find_or_create_for_inbound ----


@pytest.mark.asyncio
async def test_find_or_create_returns_existing_open_conversation() -> None:
    existing = _conv()
    conv_repo = MagicMock()
    conv_repo.find_open_by_channel_customer = AsyncMock(return_value=existing)
    conv_repo.touch_last_activity = AsyncMock()
    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    result = await svc.find_or_create_for_inbound(
        tenant_id=existing.tenant_id,
        channel_id=existing.channel_id,
        customer_external_id=existing.customer_external_id,
    )
    assert result is existing
    conv_repo.find_open_by_channel_customer.assert_awaited_once_with(
        channel_id=existing.channel_id,
        customer_external_id=existing.customer_external_id,
    )
    conv_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_find_or_create_touches_last_activity_on_existing() -> None:
    existing = _conv()
    conv_repo = MagicMock()
    conv_repo.find_open_by_channel_customer = AsyncMock(return_value=existing)
    conv_repo.touch_last_activity = AsyncMock()
    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    await svc.find_or_create_for_inbound(
        tenant_id=existing.tenant_id,
        channel_id=existing.channel_id,
        customer_external_id=existing.customer_external_id,
    )
    conv_repo.touch_last_activity.assert_awaited_once_with(
        conversation_id=existing.id,
        at=FROZEN_NOW,
    )


@pytest.mark.asyncio
async def test_find_or_create_creates_new_when_no_open() -> None:
    conv_repo = MagicMock()
    conv_repo.find_open_by_channel_customer = AsyncMock(return_value=None)
    conv_repo.create = AsyncMock(side_effect=lambda *, conversation: conversation)

    tenant_id = new_id()
    channel_id = new_id()
    customer_external_id = "ou_new_user"

    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    result = await svc.find_or_create_for_inbound(
        tenant_id=tenant_id,
        channel_id=channel_id,
        customer_external_id=customer_external_id,
    )

    assert result.tenant_id == tenant_id
    assert result.channel_id == channel_id
    assert result.customer_external_id == customer_external_id
    assert result.status == ConversationStatus.OPEN
    assert result.ai_handling is True
    assert result.assigned_agent_id is None
    assert result.opened_at == FROZEN_NOW
    assert result.last_activity_at == FROZEN_NOW
    assert len(result.id) == 26  # ULID

    conv_repo.create.assert_awaited_once()
    create_kwargs = conv_repo.create.await_args.kwargs
    assert create_kwargs["conversation"] is result


@pytest.mark.asyncio
async def test_find_or_create_for_inbound_returns_none_when_existing_is_other_tenant() -> None:
    """Defence-in-depth: an existing row from another tenant must not be returned."""
    existing = _conv(tenant_id="t_other")
    conv_repo = MagicMock()
    conv_repo.find_open_by_channel_customer = AsyncMock(return_value=existing)
    conv_repo.touch_last_activity = AsyncMock()
    conv_repo.create = AsyncMock()

    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    result = await svc.find_or_create_for_inbound(
        tenant_id="t_self",
        channel_id=existing.channel_id,
        customer_external_id=existing.customer_external_id,
    )
    assert result is None
    # And we did NOT touch it or create a new one
    conv_repo.touch_last_activity.assert_not_called()
    conv_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_find_or_create_for_inbound_resolves_race_on_integrity_error() -> None:
    """If create() raises IntegrityError (concurrent insert), retry via find_open."""
    from sqlalchemy.exc import IntegrityError

    winning = _conv(id="c_winner", tenant_id="t1", channel_id="ch1", customer_external_id="user1")
    conv_repo = MagicMock()
    # First find returns None; create raises IntegrityError; second find returns the winning row.
    conv_repo.find_open_by_channel_customer = AsyncMock(side_effect=[None, winning])
    conv_repo.create = AsyncMock(
        side_effect=IntegrityError("statement", "params", "orig")
    )
    conv_repo.touch_last_activity = AsyncMock()

    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    result = await svc.find_or_create_for_inbound(
        tenant_id="t1", channel_id="ch1", customer_external_id="user1"
    )

    assert result is winning
    assert conv_repo.find_open_by_channel_customer.await_count == 2
    conv_repo.create.assert_awaited_once()
    conv_repo.touch_last_activity.assert_awaited_once_with(
        conversation_id="c_winner", at=FROZEN_NOW
    )


@pytest.mark.asyncio
async def test_find_or_create_for_inbound_reraises_when_winner_is_other_tenant() -> None:
    """If IntegrityError fires and the winning row belongs to another tenant, re-raise."""
    from sqlalchemy.exc import IntegrityError

    conv_repo = MagicMock()
    conv_repo.find_open_by_channel_customer = AsyncMock(
        side_effect=[None, _conv(tenant_id="t_other")]
    )
    conv_repo.create = AsyncMock(
        side_effect=IntegrityError("statement", "params", "orig")
    )
    conv_repo.touch_last_activity = AsyncMock()

    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    with pytest.raises(IntegrityError):
        await svc.find_or_create_for_inbound(
            tenant_id="t1", channel_id="ch1", customer_external_id="user1"
        )


# ---- list_for_tenant ----


@pytest.mark.asyncio
async def test_list_for_tenant_passes_through() -> None:
    conv_repo = MagicMock()
    expected = [_conv(), _conv()]
    conv_repo.list_by_tenant = AsyncMock(return_value=expected)
    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    result = await svc.list_for_tenant(
        tenant_id="t1", status=ConversationStatus.PENDING, limit=10, offset=5
    )
    assert result == expected
    conv_repo.list_by_tenant.assert_awaited_once_with(
        tenant_id="t1",
        status=ConversationStatus.PENDING,
        limit=10,
        offset=5,
    )


# ---- list_for_agent ----


@pytest.mark.asyncio
async def test_list_for_agent_filters_cross_tenant() -> None:
    tenant_a = new_id()
    tenant_b = new_id()
    rows = [
        _conv(tenant_id=tenant_a, assigned_agent_id="u_agent"),
        _conv(tenant_id=tenant_b, assigned_agent_id="u_agent"),
        _conv(tenant_id=tenant_a, assigned_agent_id="u_agent"),
    ]
    conv_repo = MagicMock()
    conv_repo.list_by_assigned_agent = AsyncMock(return_value=rows)
    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    result = await svc.list_for_agent(
        tenant_id=tenant_a, agent_id="u_agent", status=ConversationStatus.PENDING
    )
    assert len(result) == 2
    assert all(r.tenant_id == tenant_a for r in result)
    conv_repo.list_by_assigned_agent.assert_awaited_once_with(
        assigned_agent_id="u_agent",
        status=ConversationStatus.PENDING,
    )


@pytest.mark.asyncio
async def test_list_for_agent_returns_empty_when_no_rows_match_tenant() -> None:
    tenant_a = new_id()
    tenant_b = new_id()
    rows = [_conv(tenant_id=tenant_b, assigned_agent_id="u_agent")]
    conv_repo = MagicMock()
    conv_repo.list_by_assigned_agent = AsyncMock(return_value=rows)
    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    result = await svc.list_for_agent(tenant_id=tenant_a, agent_id="u_agent")
    assert result == []


# ---- get ----


@pytest.mark.asyncio
async def test_get_returns_none_for_wrong_tenant() -> None:
    conv_repo = MagicMock()
    conv_repo.get_by_id = AsyncMock(return_value=_conv(tenant_id="t1"))
    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    result = await svc.get(tenant_id="t2", conversation_id="c1")
    assert result is None


@pytest.mark.asyncio
async def test_get_returns_conversation_for_correct_tenant() -> None:
    conv = _conv(tenant_id="t1")
    conv_repo = MagicMock()
    conv_repo.get_by_id = AsyncMock(return_value=conv)
    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    result = await svc.get(tenant_id="t1", conversation_id=conv.id)
    assert result is conv


@pytest.mark.asyncio
async def test_get_returns_none_when_missing() -> None:
    conv_repo = MagicMock()
    conv_repo.get_by_id = AsyncMock(return_value=None)
    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    result = await svc.get(tenant_id="t1", conversation_id="missing")
    assert result is None


# ---- assign_to_agent ----


@pytest.mark.asyncio
async def test_assign_to_agent_transitions_to_pending_with_agent() -> None:
    conv = _conv(tenant_id="t1", ai_handling=True, assigned_agent_id=None)
    conv_repo = MagicMock()
    conv_repo.get_by_id = AsyncMock(return_value=conv)
    conv_repo.update = AsyncMock(side_effect=lambda c: c)
    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    result = await svc.assign_to_agent(
        tenant_id="t1", conversation_id=conv.id, agent_id="u_agent"
    )

    assert result is not None
    assert result.status == ConversationStatus.PENDING
    assert result.assigned_agent_id == "u_agent"
    assert result.ai_handling is False
    assert result.last_activity_at == FROZEN_NOW
    conv_repo.update.assert_awaited_once()
    assert conv_repo.update.await_args.args[0] is result


@pytest.mark.asyncio
async def test_assign_to_agent_returns_none_for_wrong_tenant() -> None:
    conv = _conv(tenant_id="t1")
    conv_repo = MagicMock()
    conv_repo.get_by_id = AsyncMock(return_value=conv)
    conv_repo.update = AsyncMock()
    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    result = await svc.assign_to_agent(
        tenant_id="t2", conversation_id=conv.id, agent_id="u_agent"
    )
    assert result is None
    conv_repo.update.assert_not_called()


# ---- return_to_ai ----


@pytest.mark.asyncio
async def test_return_to_ai_resets_state() -> None:
    conv = _conv(
        tenant_id="t1",
        status=ConversationStatus.PENDING,
        assigned_agent_id="u_agent",
        ai_handling=False,
    )
    conv_repo = MagicMock()
    conv_repo.get_by_id = AsyncMock(return_value=conv)
    conv_repo.update = AsyncMock(side_effect=lambda c: c)
    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    result = await svc.return_to_ai(tenant_id="t1", conversation_id=conv.id)

    assert result is not None
    assert result.status == ConversationStatus.OPEN
    assert result.assigned_agent_id is None
    assert result.ai_handling is True
    assert result.last_activity_at == FROZEN_NOW


@pytest.mark.asyncio
async def test_return_to_ai_returns_none_for_wrong_tenant() -> None:
    conv = _conv(tenant_id="t1")
    conv_repo = MagicMock()
    conv_repo.get_by_id = AsyncMock(return_value=conv)
    conv_repo.update = AsyncMock()
    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    result = await svc.return_to_ai(tenant_id="t_other", conversation_id=conv.id)
    assert result is None
    conv_repo.update.assert_not_called()


# ---- close ----


@pytest.mark.asyncio
async def test_close_sets_status_closed_and_keeps_agent() -> None:
    conv = _conv(
        tenant_id="t1",
        status=ConversationStatus.PENDING,
        assigned_agent_id="u_agent",
        ai_handling=False,
    )
    conv_repo = MagicMock()
    conv_repo.get_by_id = AsyncMock(return_value=conv)
    conv_repo.update = AsyncMock(side_effect=lambda c: c)
    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    result = await svc.close(tenant_id="t1", conversation_id=conv.id)

    assert result is not None
    assert result.status == ConversationStatus.CLOSED
    assert result.ai_handling is False
    # assigned_agent_id is preserved per spec
    assert result.assigned_agent_id == "u_agent"
    assert result.last_activity_at == FROZEN_NOW


@pytest.mark.asyncio
async def test_close_returns_none_for_wrong_tenant() -> None:
    conv = _conv(tenant_id="t1")
    conv_repo = MagicMock()
    conv_repo.get_by_id = AsyncMock(return_value=conv)
    conv_repo.update = AsyncMock()
    svc, _, _ = _service_with_repos(conversation_repo=conv_repo)

    result = await svc.close(tenant_id="t_other", conversation_id=conv.id)
    assert result is None
    conv_repo.update.assert_not_called()


# ---- record_message ----


@pytest.mark.asyncio
async def test_record_message_creates_and_touches() -> None:
    conv = _conv(tenant_id="t1")
    conv_repo = MagicMock()
    conv_repo.get_by_id = AsyncMock(return_value=conv)
    conv_repo.touch_last_activity = AsyncMock()

    msg_repo = MagicMock()
    msg_repo.create = AsyncMock(side_effect=lambda *, message: message)

    svc, _, _ = _service_with_repos(
        conversation_repo=conv_repo, message_repo=msg_repo
    )

    result = await svc.record_message(
        tenant_id="t1",
        conversation_id=conv.id,
        role=MessageRole.CUSTOMER,
        content_text="hello",
        sender_id="u_cust",
        content_blocks={"type": "text"},
        tool_calls={"name": "search"},
    )

    assert isinstance(result, Message)
    assert result.conversation_id == conv.id
    assert result.role == MessageRole.CUSTOMER
    assert result.content_text == "hello"
    assert result.sender_id == "u_cust"
    assert result.content_blocks_json == {"type": "text"}
    assert result.tool_calls_json == {"name": "search"}
    assert result.created_at == FROZEN_NOW

    msg_repo.create.assert_awaited_once()
    conv_repo.touch_last_activity.assert_awaited_once_with(
        conversation_id=conv.id, at=FROZEN_NOW
    )


@pytest.mark.asyncio
async def test_record_message_raises_value_error_for_wrong_tenant() -> None:
    conv = _conv(tenant_id="t1")
    conv_repo = MagicMock()
    conv_repo.get_by_id = AsyncMock(return_value=conv)
    conv_repo.touch_last_activity = AsyncMock()

    msg_repo = MagicMock()
    msg_repo.create = AsyncMock()

    svc, _, _ = _service_with_repos(
        conversation_repo=conv_repo, message_repo=msg_repo
    )

    with pytest.raises(ValueError, match="tenant"):
        await svc.record_message(
            tenant_id="t_other",
            conversation_id=conv.id,
            role=MessageRole.CUSTOMER,
            content_text="hi",
        )
    msg_repo.create.assert_not_called()
    conv_repo.touch_last_activity.assert_not_called()


@pytest.mark.asyncio
async def test_record_message_uses_same_timestamp_for_message_and_touch() -> None:
    """The message created_at and the touch must use the SAME timestamp from clock()."""
    conv = _conv(tenant_id="t1")
    conv_repo = MagicMock()
    conv_repo.get_by_id = AsyncMock(return_value=conv)
    conv_repo.touch_last_activity = AsyncMock()

    captured_message: Message | None = None

    async def capture_create(*, message: Message) -> Message:
        nonlocal captured_message
        captured_message = message
        return message

    msg_repo = MagicMock()
    msg_repo.create = AsyncMock(side_effect=capture_create)

    svc, _, _ = _service_with_repos(
        conversation_repo=conv_repo, message_repo=msg_repo
    )

    await svc.record_message(
        tenant_id="t1",
        conversation_id=conv.id,
        role=MessageRole.CUSTOMER,
        content_text="hi",
    )
    assert captured_message is not None
    assert captured_message.created_at == FROZEN_NOW
    touch_call = conv_repo.touch_last_activity.await_args.kwargs
    assert touch_call["at"] == FROZEN_NOW


# ---- list_messages ----


@pytest.mark.asyncio
async def test_list_messages_returns_none_for_wrong_tenant() -> None:
    conv = _conv(tenant_id="t1")
    conv_repo = MagicMock()
    conv_repo.get_by_id = AsyncMock(return_value=conv)
    msg_repo = MagicMock()
    msg_repo.list_by_conversation = AsyncMock(return_value=[_msg(), _msg()])
    svc, _, _ = _service_with_repos(
        conversation_repo=conv_repo, message_repo=msg_repo
    )

    result = await svc.list_messages(
        tenant_id="t_other", conversation_id=conv.id
    )
    assert result is None
    msg_repo.list_by_conversation.assert_not_called()


@pytest.mark.asyncio
async def test_list_messages_paginates_via_before() -> None:
    conv = _conv(tenant_id="t1")
    conv_repo = MagicMock()
    conv_repo.get_by_id = AsyncMock(return_value=conv)
    expected = [_msg()]
    msg_repo = MagicMock()
    msg_repo.list_by_conversation = AsyncMock(return_value=expected)
    svc, _, _ = _service_with_repos(
        conversation_repo=conv_repo, message_repo=msg_repo
    )

    cutoff = datetime(2026, 9, 10, 11, 0, 0, tzinfo=UTC)
    result = await svc.list_messages(
        tenant_id="t1",
        conversation_id=conv.id,
        before=cutoff,
        limit=25,
    )
    assert result == expected
    msg_repo.list_by_conversation.assert_awaited_once_with(
        conversation_id=conv.id, before=cutoff, limit=25
    )


@pytest.mark.asyncio
async def test_list_messages_returns_none_when_conversation_missing() -> None:
    conv_repo = MagicMock()
    conv_repo.get_by_id = AsyncMock(return_value=None)
    msg_repo = MagicMock()
    msg_repo.list_by_conversation = AsyncMock()
    svc, _, _ = _service_with_repos(
        conversation_repo=conv_repo, message_repo=msg_repo
    )

    result = await svc.list_messages(tenant_id="t1", conversation_id="missing")
    assert result is None
    msg_repo.list_by_conversation.assert_not_called()
