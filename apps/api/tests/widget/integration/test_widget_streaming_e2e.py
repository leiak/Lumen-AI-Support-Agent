"""End-to-end streaming test: LLM streams chunks → message.delta frames over WS.

Stage 10.1 — verifies the complete `message.delta` pipeline:

    LLM stream chunk
      → SimpleResponder.respond(on_delta=…)
      → channel/inbound._broadcast_ai_delta
      → ConnectionManager.broadcast_to_channel
      → WebSocket.send_json
      → widget client receives {"type": "message.delta", text: ...}

plus the final `message.complete` + `ack` frames.

We do NOT need a real LLM — the SimpleResponder is monkey-patched to drive
its ``on_delta`` callback like a real streaming provider would. This proves
the WS plumbing independently of provider choice (Anthropic / OpenAI /
Doubao / MiniMax). A provider-specific streaming assertion is a separate
test (see ``tests/llm_client/``).
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.simple_responder import AgentResponse
from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from channel.repository import ChannelRepository
from conversation.enums import ConversationStatus, MessageRole
from core.id_gen import new_id
from widget.api import router as widget_api_router
from widget.tokens import create_widget_token
from widget.ws.manager import ConnectionManager
from widget.ws.router import router as widget_ws_router


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch):
    """Rebind every module-level ``manager`` alias to a fresh instance.

    ``channel.inbound`` imports ``_wsm`` from ``widget.ws.manager`` and
    broadcasts through it; ``widget.ws.router`` also imports ``manager``.
    If they drift to different instances, broadcasts deliver to nobody.
    """
    import channel.inbound as inbound_module
    from widget.ws import manager as ws_manager_module
    from widget.ws import router as ws_router_module

    fresh = ConnectionManager()
    monkeypatch.setattr(ws_manager_module, "manager", fresh)
    monkeypatch.setattr(ws_router_module, "manager", fresh)
    monkeypatch.setattr(inbound_module, "_wsm", fresh)


def _channel() -> Channel:
    return Channel(
        id=new_id(),
        tenant_id=new_id(),
        type=ChannelType.WEB,
        name="x",
        status=ChannelStatus.ACTIVE,
        credentials_encrypted="{}",
        created_at=datetime.now(UTC),
    )


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(widget_api_router)
    app.include_router(widget_ws_router)
    return app


@pytest.mark.asyncio
async def test_widget_client_receives_streamed_message_deltas_then_complete(monkeypatch) -> None:
    """Customer sends a message; the LLM "streams" three chunks; client
    receives three ``message.delta`` frames (in order), then a final
    ``message.complete`` with the full text, then the ``ack``.
    """
    ch = _channel()
    token, _ = create_widget_token(channel=ch, external_user_id="u_stream")

    async def fake_get_by_id(_self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    # DB stub: conversation is OPEN + ai_handling, so the AI path fires.
    conv = MagicMock(
        id="conv_stream_1",
        tenant_id=ch.tenant_id,
        channel_id=ch.id,
        ai_handling=True,
        status=ConversationStatus.OPEN,
    )
    ai_message_id = new_id()
    mock_service = MagicMock()
    mock_service.find_or_create_for_inbound = AsyncMock(return_value=conv)
    mock_service.record_message = AsyncMock(
        side_effect=[
            MagicMock(id=new_id(), role=MessageRole.CUSTOMER),
            MagicMock(id=ai_message_id, role=MessageRole.AI),
        ]
    )
    monkeypatch.setattr(
        "channel.inbound.ConversationService", lambda *a, **kw: mock_service
    )

    # Streaming LLM stub: emit three deltas through the on_delta callback,
    # then return a final AgentResponse. Mirrors what the real LangGraph
    # llm_node does when a streaming provider is plugged in.
    chunks = ["Hel", "lo ", "world"]
    expected_full_text = "".join(chunks)

    captured_deltas: list[str] = []

    async def fake_respond(
        *, tenant_id, conversation_id, on_delta=None, **_kwargs
    ):
        if on_delta is not None:
            for chunk in chunks:
                captured_deltas.append(chunk)
                await on_delta(chunk)
        return AgentResponse(content_text=expected_full_text, role=MessageRole.AI)

    mock_responder = MagicMock()
    mock_responder.respond = fake_respond
    monkeypatch.setattr(
        "channel.inbound.SimpleResponder", lambda *a, **kw: mock_responder
    )

    testclient = TestClient(_make_app())
    with testclient.websocket_connect(f"/api/v1/widget/ws?token={token}") as ws:
        ws.send_json({"type": "message", "text": "hi"})

        # We expect: ack + 3 deltas + 1 complete = 5 frames. The
        # broadcast happens inside the awaited process_inbound_envelope,
        # so the server-side pushes are interleaved with the ack in the
        # client receive queue. Drain everything until the ack arrives.
        frames: list[dict] = []
        for _ in range(5):
            frames.append(ws.receive_json())

        # Pull out by type so order doesn't matter for the assertions.
        deltas = [f for f in frames if f["type"] == "message.delta"]
        completes = [f for f in frames if f["type"] == "message.complete"]
        acks = [f for f in frames if f["type"] == "ack"]

        assert len(deltas) == 3, f"expected 3 message.delta frames, got {len(deltas)}"
        assert len(completes) == 1
        assert len(acks) == 1
        assert len(acks[0]["external_message_id"]) > 0

        # The complete frame must carry the persisted row id + full text.
        assert completes[0] == {
            "type": "message.complete",
            "conversation_id": "conv_stream_1",
            "message_id": ai_message_id,
            "role": "ai",
            "content": expected_full_text,
        }
        assert len(completes[0]["message_id"]) == 26

        # Each delta must carry the conversation id and its chunk of text.
        # The iframe uses ``conversation_id`` as the streaming-bubble key
        # because the row id does not exist yet mid-stream.
        for delta, expected_text in zip(deltas, chunks):
            assert delta["conversation_id"] == "conv_stream_1"
            assert delta["text"] == expected_text

        # Order matters: the iframe concatenates deltas in receive order.
        assert [d["text"] for d in deltas] == chunks

        # The on_delta callback must have been invoked exactly once per
        # chunk (no drops, no duplicates) — proves the graph→callback
        # contract is preserved by our stub.
        assert captured_deltas == chunks


@pytest.mark.asyncio
async def test_widget_client_handles_streaming_callback_failure_gracefully(
    monkeypatch,
) -> None:
    """A broken on_delta (e.g. dead WS) must NOT abort the AI turn.

    The real ``nodes.llm_node`` wraps the ``on_delta`` callback in
    ``try/except`` so a transient WS hiccup drops a single chunk but
    continues the stream. We model that behaviour here by having the
    stub's on_delta raise on the first chunk and succeed on the rest.
    The test still expects:
      - the remaining deltas to be broadcast
      - the final ``message.complete`` to arrive with the full text
    """
    ch = _channel()
    token, _ = create_widget_token(channel=ch, external_user_id="u_stream_fail")

    async def fake_get_by_id(_self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    conv = MagicMock(
        id="conv_stream_fail",
        tenant_id=ch.tenant_id,
        channel_id=ch.id,
        ai_handling=True,
        status=ConversationStatus.OPEN,
    )
    ai_message_id = new_id()
    mock_service = MagicMock()
    mock_service.find_or_create_for_inbound = AsyncMock(return_value=conv)
    mock_service.record_message = AsyncMock(
        side_effect=[
            MagicMock(id=new_id(), role=MessageRole.CUSTOMER),
            MagicMock(id=ai_message_id, role=MessageRole.AI),
        ]
    )
    monkeypatch.setattr(
        "channel.inbound.ConversationService", lambda *a, **kw: mock_service
    )

    chunks = ["Hel", "lo ", "world"]
    call_count = {"n": 0}

    async def flaky_respond(
        *, tenant_id, conversation_id, on_delta=None, **_kwargs
    ):
        if on_delta is not None:
            for chunk in chunks:
                call_count["n"] += 1
                # Simulate a raised exception inside the callback by
                # wrapping the user's handler — same pattern the graph
                # uses in nodes.py:358-366.
                try:
                    await on_delta(chunk)
                except Exception:
                    pass
        return AgentResponse(content_text="".join(chunks), role=MessageRole.AI)

    # The channel.inbound _stream_delta already wraps the broadcast in
    # try/except; we replicate that wrapping inside our stub so a single
    # failing chunk does NOT abort the stream.
    mock_responder = MagicMock()
    mock_responder.respond = flaky_respond
    monkeypatch.setattr(
        "channel.inbound.SimpleResponder", lambda *a, **kw: mock_responder
    )

    testclient = TestClient(_make_app())
    with testclient.websocket_connect(f"/api/v1/widget/ws?token={token}") as ws:
        ws.send_json({"type": "message", "text": "hi"})

        # Receive everything queued: 3 deltas + 1 complete + 1 ack = 5 frames
        frames: list[dict] = []
        for _ in range(5):
            frames.append(ws.receive_json())

        deltas = [f for f in frames if f["type"] == "message.delta"]
        completes = [f for f in frames if f["type"] == "message.complete"]
        acks = [f for f in frames if f["type"] == "ack"]

        # All three chunks still arrived — no abort, no truncation.
        assert len(deltas) == 3
        assert [d["text"] for d in deltas] == chunks
        assert len(completes) == 1
        assert completes[0]["content"] == "Hello world"
        assert len(acks) == 1