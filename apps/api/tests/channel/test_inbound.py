"""Tests for channel.inbound.process_inbound_envelope."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from agent.simple_responder import AgentResponse
from channel.enums import ChannelType
from channel.inbound import process_inbound_envelope
from channel.messages import MessageEnvelope
from conversation.enums import ConversationStatus, MessageRole


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
async def test_process_inbound_creates_conv_and_records_message() -> None:
    """Happy path: envelope -> find_or_create + record_message both called."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(id="c1", tenant_id="t1")
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        mock_service.record_message = AsyncMock()

        await process_inbound_envelope(_envelope())

        mock_service.find_or_create_for_inbound.assert_awaited_once_with(
            tenant_id="t1",
            channel_id="ch1",
            customer_external_id="user1",
        )
        mock_service.record_message.assert_awaited_once_with(
            tenant_id="t1",
            conversation_id="c1",
            role=MessageRole.CUSTOMER,
            content_text="hello",
        )


@pytest.mark.asyncio
async def test_process_inbound_drops_on_cross_tenant() -> None:
    """find_or_create returns None -> log + drop, no record_message."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls:
        mock_service = mock_svc_cls.return_value
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=None)
        mock_service.record_message = AsyncMock()

        await process_inbound_envelope(_envelope())

        mock_service.record_message.assert_not_called()


@pytest.mark.asyncio
async def test_process_inbound_swallows_persistence_error() -> None:
    """DB error is logged but does NOT propagate — webhook still ACKs."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls:
        mock_service = mock_svc_cls.return_value
        mock_service.find_or_create_for_inbound = AsyncMock(
            side_effect=RuntimeError("db down")
        )

        # Must not raise
        await process_inbound_envelope(_envelope())
        mock_service.record_message.assert_not_called()


@pytest.mark.asyncio
async def test_process_inbound_swallows_record_message_error() -> None:
    """An error in record_message is also logged + swallowed."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(id="c1", tenant_id="t1")
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        mock_service.record_message = AsyncMock(side_effect=RuntimeError("write fail"))

        # Must not raise
        await process_inbound_envelope(_envelope())


@pytest.mark.asyncio
async def test_process_inbound_does_not_log_message_content() -> None:
    """The processor must not include message text in any log extra (PII safety)."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.logger") as mock_logger:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(id="c1", tenant_id="t1")
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        mock_service.record_message = AsyncMock()

        secret_text = "SECRET_PII_TOKEN_DO_NOT_LOG"  # noqa: S105
        await process_inbound_envelope(_envelope(text=secret_text))

        # Inspect every call's extra dict — none should contain the text.
        for call in mock_logger.info.call_args_list + mock_logger.warning.call_args_list + \
                mock_logger.exception.call_args_list:
            kwargs = call.kwargs or {}
            extra = kwargs.get("extra", {})
            assert secret_text not in str(extra), (
                f"text leaked into log extra: {extra}"
            )


@pytest.mark.asyncio
async def test_process_inbound_triggers_ai_response_when_open_and_ai_handling() -> None:
    """When conv is OPEN + ai_handling, SimpleResponder is called and AI msg recorded."""
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

        mock_responder = mock_resp_cls.return_value
        mock_responder.respond = AsyncMock(
            return_value=AgentResponse(content_text="AI says hi", role=MessageRole.AI)
        )

        await process_inbound_envelope(_envelope())

        assert mock_responder.respond.await_count == 1
        call_kwargs = mock_responder.respond.await_args.kwargs
        assert call_kwargs["tenant_id"] == "t1"
        assert call_kwargs["conversation_id"] == "c1"
        # A streaming relay callback is wired so message.delta frames flow.
        assert callable(call_kwargs["on_delta"])
        # record_message called twice: customer, then AI.
        assert mock_service.record_message.await_count == 2
        second_call = mock_service.record_message.await_args_list[1]
        assert second_call.kwargs["role"] == MessageRole.AI
        assert second_call.kwargs["content_text"] == "AI says hi"


@pytest.mark.asyncio
async def test_process_inbound_skips_ai_response_when_human_handling() -> None:
    """When ai_handling=False, SimpleResponder must not be called."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.SimpleResponder") as mock_resp_cls:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1",
            tenant_id="t1",
            ai_handling=False,
            status=ConversationStatus.PENDING,
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        mock_service.record_message = AsyncMock()

        mock_responder = mock_resp_cls.return_value

        await process_inbound_envelope(_envelope())

        mock_responder.respond.assert_not_called()
        # Only customer message recorded, no AI.
        assert mock_service.record_message.await_count == 1


@pytest.mark.asyncio
async def test_process_inbound_skips_ai_response_when_responder_returns_none() -> None:
    """When SimpleResponder returns None, inbound still completes cleanly."""
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

        mock_responder = mock_resp_cls.return_value
        mock_responder.respond = AsyncMock(return_value=None)

        await process_inbound_envelope(_envelope())

        # Only customer message recorded.
        assert mock_service.record_message.await_count == 1


@pytest.mark.asyncio
async def test_process_inbound_swallows_ai_responder_exception() -> None:
    """If the AI responder raises, the inbound pipeline must not propagate."""
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

        mock_responder = mock_resp_cls.return_value
        mock_responder.respond = AsyncMock(side_effect=RuntimeError("boom"))

        # Must not raise.
        await process_inbound_envelope(_envelope())

        # Only the customer message was recorded; AI attempt failed.
        assert mock_service.record_message.await_count == 1


# ---- Task 5.4: WS message.complete push ----


@pytest.mark.asyncio
async def test_process_inbound_broadcasts_message_complete_to_ws() -> None:
    """AI message persistence triggers a message.complete WS broadcast."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.SimpleResponder") as mock_resp_cls, \
         patch("channel.inbound._broadcast_ai_complete", new_callable=AsyncMock) as mock_bcast:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1",
            tenant_id="t1",
            ai_handling=True,
            status=ConversationStatus.OPEN,
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)

        ai_msg = MagicMock(id="m_ai_1", role=MessageRole.AI, content_text="AI reply")
        mock_service.record_message = AsyncMock(side_effect=[MagicMock(), ai_msg])

        mock_responder = mock_resp_cls.return_value
        mock_responder.respond = AsyncMock(
            return_value=AgentResponse(content_text="AI reply", role=MessageRole.AI)
        )

        await process_inbound_envelope(_envelope(channel_id="ch1"))

        mock_bcast.assert_awaited_once_with(
            channel_id="ch1",
            conversation_id="c1",
            message_id="m_ai_1",
            role=MessageRole.AI,
            content="AI reply",
        )


@pytest.mark.asyncio
async def test_process_inbound_broadcast_uses_persisted_message_id() -> None:
    """The pushed message_id must be the persisted row id, not a fresh ULID.

    The frontend dedupes the WS push against a follow-up REST fetch, so the
    two must agree on the id.
    """
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.SimpleResponder") as mock_resp_cls, \
         patch("channel.inbound._broadcast_ai_complete", new_callable=AsyncMock) as mock_bcast:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1", tenant_id="t1", ai_handling=True, status=ConversationStatus.OPEN
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)

        persisted = MagicMock(id="PERSISTED_ROW_ID", role=MessageRole.AI)
        mock_service.record_message = AsyncMock(side_effect=[MagicMock(), persisted])

        mock_responder = mock_resp_cls.return_value
        mock_responder.respond = AsyncMock(
            return_value=AgentResponse(content_text="x", role=MessageRole.AI)
        )

        await process_inbound_envelope(_envelope())

        assert mock_bcast.await_args.kwargs["message_id"] == "PERSISTED_ROW_ID"


@pytest.mark.asyncio
async def test_process_inbound_streams_message_delta_via_relay_callback() -> None:
    """The on_delta callback handed to SimpleResponder must fan each chunk
    out as a message.delta broadcast (keyed by conversation_id, no row id yet).
    """
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.SimpleResponder") as mock_resp_cls, \
         patch("channel.inbound._broadcast_ai_delta", new_callable=AsyncMock) as mock_delta:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1",
            tenant_id="t1",
            channel_id="ch1",
            ai_handling=True,
            status=ConversationStatus.OPEN,
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        ai_msg = MagicMock(id="m_ai_1", role=MessageRole.AI, content_text="Hel lo")
        mock_service.record_message = AsyncMock(side_effect=[MagicMock(), ai_msg])

        mock_responder = mock_resp_cls.return_value
        mock_responder.respond = AsyncMock(
            return_value=AgentResponse(content_text="Hel lo", role=MessageRole.AI)
        )

        await process_inbound_envelope(_envelope(channel_id="ch1"))

        # Response was invoked with a relay callback that fans deltas out.
        on_delta = mock_responder.respond.await_args.kwargs["on_delta"]
        await on_delta("Hel")
        await on_delta(" lo")

        # Each chunk is fanned out as its own message.delta broadcast.
        mock_delta.assert_has_awaits(
            [
                call(channel_id="ch1", conversation_id="c1", text="Hel"),
                call(channel_id="ch1", conversation_id="c1", text=" lo"),
            ]
        )
        assert mock_delta.await_count == 2


@pytest.mark.asyncio
async def test_process_inbound_skips_broadcast_when_no_ai_response() -> None:
    """If AI returns None (e.g. conversation not ai_handling), no broadcast."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound._broadcast_ai_complete", new_callable=AsyncMock) as mock_bcast:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1",
            tenant_id="t1",
            ai_handling=False,
            status=ConversationStatus.PENDING,
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        mock_service.record_message = AsyncMock()

        await process_inbound_envelope(_envelope())

        mock_bcast.assert_not_called()


@pytest.mark.asyncio
async def test_process_inbound_skips_broadcast_on_cross_tenant_probe() -> None:
    """A blocked cross-tenant probe must not emit anything onto the wire."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound._broadcast_ai_complete", new_callable=AsyncMock) as mock_bcast:
        mock_service = mock_svc_cls.return_value
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=None)
        mock_service.record_message = AsyncMock()

        await process_inbound_envelope(_envelope())

        mock_bcast.assert_not_called()


@pytest.mark.asyncio
async def test_process_inbound_swallows_broadcast_failure() -> None:
    """WS broadcast failure does not break the inbound path."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.SimpleResponder") as mock_resp_cls, \
         patch("channel.inbound._broadcast_ai_complete", new_callable=AsyncMock) as mock_bcast:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1",
            tenant_id="t1",
            ai_handling=True,
            status=ConversationStatus.OPEN,
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)

        ai_msg = MagicMock(id="m_ai_1", role=MessageRole.AI, content_text="reply")
        mock_service.record_message = AsyncMock(side_effect=[MagicMock(), ai_msg])

        mock_responder = mock_resp_cls.return_value
        mock_responder.respond = AsyncMock(
            return_value=AgentResponse(content_text="reply", role=MessageRole.AI)
        )
        mock_bcast.side_effect = RuntimeError("WS down")

        # Must not raise — the AI message is already durably persisted.
        await process_inbound_envelope(_envelope())

        assert mock_service.record_message.await_count == 2
