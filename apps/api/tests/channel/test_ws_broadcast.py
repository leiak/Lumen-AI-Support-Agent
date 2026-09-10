"""Tests for the WS broadcast helper (`channel.inbound._broadcast_ai_complete`).

These pin the wire format of the `message.complete` frame. Stage 7 will add
`message.delta` frames ahead of it; the completion frame's shape must stay
backwards-compatible for already-deployed widgets.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from channel.inbound import _broadcast_ai_complete
from conversation.enums import MessageRole


@pytest.mark.asyncio
async def test_broadcast_ai_complete_calls_manager_with_correct_payload() -> None:
    """Verify the broadcast payload structure matches the WS protocol."""
    with patch("channel.inbound._wsm") as mock_manager:
        mock_manager.broadcast_to_channel = AsyncMock(return_value=1)

        await _broadcast_ai_complete(
            channel_id="ch1",
            conversation_id="c1",
            message_id="m1",
            role=MessageRole.AI,
            content="hello",
        )

        mock_manager.broadcast_to_channel.assert_awaited_once_with(
            channel_id="ch1",
            payload={
                "type": "message.complete",
                "conversation_id": "c1",
                "message_id": "m1",
                "role": "ai",
                "content": "hello",
            },
        )


@pytest.mark.asyncio
async def test_broadcast_ai_complete_serialises_role_as_plain_string() -> None:
    """`role` must be a JSON-serialisable str, not a bare enum member."""
    with patch("channel.inbound._wsm") as mock_manager:
        mock_manager.broadcast_to_channel = AsyncMock(return_value=1)

        await _broadcast_ai_complete(
            channel_id="ch1",
            conversation_id="c1",
            message_id="m1",
            role=MessageRole.AI,
            content="hello",
        )

        role = mock_manager.broadcast_to_channel.await_args.kwargs["payload"]["role"]
        assert role == "ai"
        assert type(role) is str


@pytest.mark.asyncio
async def test_broadcast_ai_complete_swallows_exception() -> None:
    """A WS broadcast exception is logged but not re-raised."""
    with patch("channel.inbound._wsm") as mock_manager:
        mock_manager.broadcast_to_channel = AsyncMock(side_effect=RuntimeError("ws down"))

        # Must not raise
        await _broadcast_ai_complete(
            channel_id="ch1",
            conversation_id="c1",
            message_id="m1",
            role=MessageRole.AI,
            content="hello",
        )


@pytest.mark.asyncio
async def test_broadcast_ai_complete_does_not_log_message_content() -> None:
    """Broadcast failures must not leak message text into logs (PII safety)."""
    secret = "SECRET_PII_TOKEN_DO_NOT_LOG"  # noqa: S105
    with patch("channel.inbound._wsm") as mock_manager, \
         patch("channel.inbound.logger") as mock_logger:
        mock_manager.broadcast_to_channel = AsyncMock(side_effect=RuntimeError("ws down"))

        await _broadcast_ai_complete(
            channel_id="ch1",
            conversation_id="c1",
            message_id="m1",
            role=MessageRole.AI,
            content=secret,
        )

        for call in mock_logger.warning.call_args_list:
            extra = (call.kwargs or {}).get("extra", {})
            assert secret not in str(extra), f"content leaked into log extra: {extra}"


@pytest.mark.asyncio
async def test_broadcast_ai_complete_returns_when_no_connections() -> None:
    """broadcast_to_channel returns 0 when no clients connected — no error."""
    with patch("channel.inbound._wsm") as mock_manager:
        mock_manager.broadcast_to_channel = AsyncMock(return_value=0)

        await _broadcast_ai_complete(
            channel_id="ch1",
            conversation_id="c1",
            message_id="m1",
            role=MessageRole.AI,
            content="hello",
        )

        mock_manager.broadcast_to_channel.assert_awaited_once()


def test_inbound_and_router_share_one_connection_manager() -> None:
    """`channel.inbound` and `widget.ws.router` MUST share one connection table.

    If each module instantiated its own ConnectionManager, every broadcast
    would deliver to zero clients while all the mocked tests above still
    passed. This is the regression guard for that failure mode.
    """
    import channel.inbound as inbound_module
    import widget.ws.manager as manager_module
    import widget.ws.router as router_module

    assert inbound_module._wsm is manager_module.manager
    assert router_module.manager is manager_module.manager
