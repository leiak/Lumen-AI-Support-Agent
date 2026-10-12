"""Regression tests for AI-failure auto-escalation on the widget + email channels.

Background
----------
When the LLM call fails (budget exceeded / provider unavailable / output
invalid / graph exception / empty final_text), the agent graph returns
``FALLBACK_MESSAGE = "抱歉,AI 助手暂时无法回复,请稍后再试或联系人工客服。"``.

Pre-fix behaviour: the fallback text was persisted as an AI message,
but the conversation row was left at ``status=OPEN, ai_handling=True,
assigned_agent_id=NULL`` — invisible to every agent's ``/inbox`` queue
AND to the admin "all conversations" view. The customer saw a polite
fallback reply, but no human ever took over.

Fix: when ``ai_response.content_text == FALLBACK_MESSAGE``, the inbound
processor must call ``conv_service.escalate_to_human_queue(tenant_id,
conversation_id)`` AFTER persisting the AI message, flipping the
conversation to ``status=PENDING, ai_handling=False, assigned_agent_id=None``
so the next agent ``/inbox`` poll sees it.

Both widget (``channel.inbound``) and email (``channel.inbound_email``)
inbound paths use the same ``responder_factory() → SimpleResponder.respond()
→ conv_service.record_message(...)`` pattern and must apply the same
flip — otherwise the email path silently regresses while the widget path
behaves correctly.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.graph.prompts import FALLBACK_MESSAGE
from agent.simple_responder import AgentResponse
from channel.enums import ChannelType
from channel.inbound import process_inbound_envelope
from channel.messages import MessageEnvelope
from channel.inbound_email import handle_email_inbound
from core.email_parser import ParsedEmail
from conversation.enums import ConversationStatus, MessageRole


# ---- Widget path: channel.inbound.process_inbound_envelope ----


def _envelope(**overrides) -> MessageEnvelope:
    base = dict(
        envelope_id="env_1",
        tenant_id="t1",
        channel_type=ChannelType.FEISHU,
        channel_id="ch1",
        external_user_id="user1",
        external_conversation_id="chat1",
        external_message_id="om_1",
        text="hello",
        received_at=datetime.now(UTC),
    )
    base.update(overrides)
    return MessageEnvelope(**base)


@pytest.mark.asyncio
async def test_widget_fallback_message_triggers_escalate() -> None:
    """Widget: FALLBACK_MESSAGE must escalate after the AI message is persisted."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.SimpleResponder") as mock_resp_cls:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1",
            tenant_id="t1",
            ai_handling=True,
            status=ConversationStatus.OPEN,
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        mock_service.record_message = AsyncMock()
        mock_service.escalate_to_human_queue = AsyncMock()

        mock_responder = mock_resp_cls.return_value
        mock_responder.respond = AsyncMock(
            return_value=AgentResponse(
                content_text=FALLBACK_MESSAGE, role=MessageRole.AI
            )
        )

        await process_inbound_envelope(_envelope())

        # The fallback text was persisted as an AI message (customer saw it).
        record_calls = [
            c for c in mock_service.record_message.await_args_list
            if c.kwargs.get("role") == MessageRole.AI
        ]
        assert len(record_calls) == 1
        assert record_calls[0].kwargs["content_text"] == FALLBACK_MESSAGE
        # ...AND the conversation was escalated to the human queue.
        mock_service.escalate_to_human_queue.assert_awaited_once_with(
            tenant_id="t1",
            conversation_id="c1",
        )


@pytest.mark.asyncio
async def test_widget_normal_response_does_not_escalate() -> None:
    """Widget: a successful AI reply must NOT trigger escalation."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.SimpleResponder") as mock_resp_cls:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1",
            tenant_id="t1",
            ai_handling=True,
            status=ConversationStatus.OPEN,
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        mock_service.record_message = AsyncMock()
        mock_service.escalate_to_human_queue = AsyncMock()

        mock_responder = mock_resp_cls.return_value
        mock_responder.respond = AsyncMock(
            return_value=AgentResponse(
                content_text="您好,有什么可以帮您?", role=MessageRole.AI
            )
        )

        await process_inbound_envelope(_envelope())

        mock_service.escalate_to_human_queue.assert_not_awaited()


@pytest.mark.asyncio
async def test_widget_escalate_failure_does_not_abort_inbound() -> None:
    """Widget: escalate_to_human_queue raising must NOT abort the inbound flow.

    The FALLBACK_MESSAGE is already durably persisted at the call site;
    failing to flip the queue is a soft miss on agent pickup, not a
    customer-facing failure. Mirror the ``_broadcast_ai_complete`` /
    ``_enqueue_qa_judge`` swallow-and-warn pattern.
    """
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.SimpleResponder") as mock_resp_cls:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1",
            tenant_id="t1",
            ai_handling=True,
            status=ConversationStatus.OPEN,
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        mock_service.record_message = AsyncMock()
        mock_service.escalate_to_human_queue = AsyncMock(
            side_effect=RuntimeError("DB blip on tenant_budget_snapshots UPSERT")
        )

        mock_responder = mock_resp_cls.return_value
        mock_responder.respond = AsyncMock(
            return_value=AgentResponse(
                content_text=FALLBACK_MESSAGE, role=MessageRole.AI
            )
        )

        # Must not raise.
        await process_inbound_envelope(_envelope())


# ---- Email path: channel.inbound_email.handle_email_inbound ----


def _parsed_email(**overrides) -> ParsedEmail:
    base = dict(
        message_id="<msg@example.com>",
        thread_id="<thread@example.com>",
        from_address="customer@example.com",
        to_address="support@example.com",
        subject="help",
        body_text="hello",
        in_reply_to=None,
        references=[],
    )
    base.update(overrides)
    return ParsedEmail(**base)


@pytest.mark.asyncio
async def test_email_fallback_message_triggers_escalate() -> None:
    """Email: same flip semantics as widget — FALLBACK_MESSAGE escalates."""
    # Bypass real DB session by stubbing the sessionmaker + the
    # conversation's existence check.
    with patch("channel.inbound_email.get_sessionmaker") as mock_sm_cls, \
         patch("channel.inbound_email._check_existing_message",
               new_callable=AsyncMock) as mock_check, \
         patch("channel.inbound_email._find_conversation_by_thread",
               new_callable=AsyncMock) as mock_find, \
         patch("channel.inbound_email.ConversationService") as mock_svc_cls:
        # Session stub
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_sm_cls.return_value = MagicMock(return_value=mock_session)

        mock_check.return_value = None  # not a duplicate
        mock_conv = MagicMock(
            id="c2",
            tenant_id="t2",
            ai_handling=True,
            status=ConversationStatus.OPEN,
        )
        mock_find.return_value = mock_conv

        mock_service = mock_svc_cls.return_value
        mock_service.record_message = AsyncMock()
        mock_service.escalate_to_human_queue = AsyncMock()

        responder = MagicMock()
        responder.respond = AsyncMock(
            return_value=AgentResponse(
                content_text=FALLBACK_MESSAGE, role=MessageRole.AI
            )
        )

        await handle_email_inbound(
            tenant_id="t2",
            parsed=_parsed_email(),
            channel_id="ch-email",
            responder_factory=lambda: responder,
            outbound=None,
        )

        mock_service.escalate_to_human_queue.assert_awaited_once_with(
            tenant_id="t2",
            conversation_id="c2",
        )


@pytest.mark.asyncio
async def test_email_normal_response_does_not_escalate() -> None:
    """Email: a successful AI reply must NOT trigger escalation."""
    with patch("channel.inbound_email.get_sessionmaker") as mock_sm_cls, \
         patch("channel.inbound_email._check_existing_message",
               new_callable=AsyncMock) as mock_check, \
         patch("channel.inbound_email._find_conversation_by_thread",
               new_callable=AsyncMock) as mock_find, \
         patch("channel.inbound_email.ConversationService") as mock_svc_cls:
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_sm_cls.return_value = MagicMock(return_value=mock_session)

        mock_check.return_value = None
        mock_conv = MagicMock(
            id="c3",
            tenant_id="t3",
            ai_handling=True,
            status=ConversationStatus.OPEN,
        )
        mock_find.return_value = mock_conv

        mock_service = mock_svc_cls.return_value
        mock_service.record_message = AsyncMock()
        mock_service.escalate_to_human_queue = AsyncMock()

        responder = MagicMock()
        responder.respond = AsyncMock(
            return_value=AgentResponse(
                content_text="您好,需要什么帮助?", role=MessageRole.AI
            )
        )

        await handle_email_inbound(
            tenant_id="t3",
            parsed=_parsed_email(),
            channel_id="ch-email",
            responder_factory=lambda: responder,
            outbound=None,
        )

        mock_service.escalate_to_human_queue.assert_not_awaited()
