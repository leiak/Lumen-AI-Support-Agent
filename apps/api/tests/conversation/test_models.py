"""Tests for Conversation and Message ORM models."""
from datetime import UTC, datetime

from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation, Message
from core.id_gen import new_id


def _tenant_id() -> str:
    return new_id()


def _channel_id() -> str:
    return new_id()


def test_conversation_can_be_constructed() -> None:
    now = datetime.now(UTC)
    conv = Conversation(
        id=new_id(),
        tenant_id=_tenant_id(),
        channel_id=_channel_id(),
        customer_external_id="ou_user_1",
        status=ConversationStatus.OPEN,
        ai_handling=True,
        opened_at=now,
        last_activity_at=now,
    )
    assert conv.status is ConversationStatus.OPEN
    assert conv.ai_handling is True
    assert conv.assigned_agent_id is None


def test_message_can_be_constructed() -> None:
    now = datetime.now(UTC)
    msg = Message(
        id=new_id(),
        conversation_id=new_id(),
        role=MessageRole.CUSTOMER,
        content_text="hello",
        sender_id=None,
        created_at=now,
    )
    assert msg.role is MessageRole.CUSTOMER
    assert msg.content_text == "hello"
    assert msg.content_blocks_json is None
    assert msg.tool_calls_json is None


def test_message_role_enum_values() -> None:
    """All five roles should be available for M1."""
    assert MessageRole.CUSTOMER.value == "customer"
    assert MessageRole.AGENT.value == "agent"
    assert MessageRole.AI.value == "ai"
    assert MessageRole.SYSTEM.value == "system"
    assert MessageRole.TOOL.value == "tool"


def test_conversation_status_enum_values() -> None:
    assert ConversationStatus.OPEN.value == "open"
    assert ConversationStatus.PENDING.value == "pending"
    assert ConversationStatus.CLOSED.value == "closed"
