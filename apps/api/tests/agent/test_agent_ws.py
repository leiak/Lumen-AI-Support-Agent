"""Tests for the agent-facing WebSocket endpoint (Stage 9.5).

Pure-Python tests — no DB. We monkeypatch
``agent.ws.ConversationService`` so the route handler resolves the
conversation against an in-memory fake, and we spy on the WS
``ConnectionManager.connect`` / ``disconnect`` methods to verify
the lifecycle without standing up a real WebSocket server.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from agent import ws as ws_module
from conversation import service as service_module
from conversation.enums import ConversationStatus
from conversation.models import Conversation
from core.id_gen import new_id


def _conv(**kwargs: Any) -> Conversation:
    base: dict[str, Any] = dict(
        id=new_id(),
        tenant_id="tenant_X",
        channel_id=new_id(),
        customer_external_id="ou_customer_1",
        status=ConversationStatus.PENDING,
        assigned_agent_id=None,
        ai_handling=False,
        opened_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
        last_activity_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    )
    base.update(kwargs)
    return Conversation(**base)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.closed_with: int | None = None
        self.received: list[str] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code

    async def receive_text(self) -> str:
        # Block forever — emulates a client that stays connected.
        import asyncio

        await asyncio.sleep(3600)
        return ""

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.received.append(str(payload))


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(ws_module.router)
    return app


async def test_ws_rejects_invalid_token_with_1008(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    """Bad JWT → close 1008, never accept."""
    fake_ws = _FakeWebSocket()

    # ``_decode_jwt`` raises HTTPException(401) on bad token; we
    # make the real call so we exercise the error path.
    async def _bad_decode(_: str) -> dict[str, Any]:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="bad token")

    monkeypatch.setattr(ws_module, "_decode_jwt", _bad_decode)
    # Patch ConversationService.get so we know it's never called.
    get_mock = AsyncMock(return_value=_conv())
    monkeypatch.setattr(service_module.ConversationService, "get", get_mock)

    await ws_module.websocket_endpoint(fake_ws, "conv-1", token="bad")  # noqa: S106

    assert fake_ws.accepted is False
    assert fake_ws.closed_with == 1008
    get_mock.assert_not_awaited()


async def test_ws_rejects_token_missing_role(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    """Valid JWT but role=viewer → close 1008."""
    fake_ws = _FakeWebSocket()

    async def _ok_decode(_: str) -> dict[str, Any]:
        return {"sub": "u_viewer", "tenant_id": "tenant_X", "role": "viewer"}

    monkeypatch.setattr(ws_module, "_decode_jwt", _ok_decode)
    get_mock = AsyncMock(return_value=_conv())
    monkeypatch.setattr(service_module.ConversationService, "get", get_mock)

    await ws_module.websocket_endpoint(fake_ws, "conv-1", token="jwt")  # noqa: S106

    assert fake_ws.accepted is False
    assert fake_ws.closed_with == 1008


async def test_ws_rejects_unknown_conversation(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    """Valid JWT, valid role, but conversation is None → close 1008."""
    fake_ws = _FakeWebSocket()

    async def _ok_decode(_: str) -> dict[str, Any]:
        return {"sub": "u_agent", "tenant_id": "tenant_X", "role": "agent"}

    monkeypatch.setattr(ws_module, "_decode_jwt", _ok_decode)
    get_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(service_module.ConversationService, "get", get_mock)

    await ws_module.websocket_endpoint(fake_ws, "conv-missing", token="jwt")  # noqa: S106

    assert fake_ws.accepted is False
    assert fake_ws.closed_with == 1008


async def test_ws_accepts_and_registers_when_valid(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    """Happy path: agent JWT, conversation resolved, manager.connect called."""
    conv = _conv(channel_id="chan-abc")
    fake_ws = _FakeWebSocket()

    async def _ok_decode(_: str) -> dict[str, Any]:
        return {"sub": "u_agent", "tenant_id": "tenant_X", "role": "agent"}

    monkeypatch.setattr(ws_module, "_decode_jwt", _ok_decode)
    monkeypatch.setattr(
        service_module.ConversationService, "get", AsyncMock(return_value=conv)
    )

    connect_mock = AsyncMock(return_value="conn-1")
    disconnect_mock = AsyncMock()
    monkeypatch.setattr(
        ws_module.manager, "connect", MagicMock(wraps=connect_mock)
    )
    # Replace the underlying connect (it's defined as `async def` on
    # the manager class). Use a sync magic mock that returns a coroutine.
    monkeypatch.setattr(
        ws_module.manager, "connect", connect_mock
    )
    monkeypatch.setattr(ws_module.manager, "disconnect", disconnect_mock)

    # Drive the endpoint then close — we want to exit the
    # ``receive_text`` sleep, so swap it for one that raises.
    async def _raise_disconnect() -> str:
        from fastapi import WebSocketDisconnect

        raise WebSocketDisconnect()

    fake_ws.receive_text = _raise_disconnect  # type: ignore[method-assign]

    await ws_module.websocket_endpoint(fake_ws, conv.id, token="jwt")  # noqa: S106

    assert fake_ws.accepted is True
    assert fake_ws.closed_with is None  # graceful path
    connect_mock.assert_awaited_once()
    # Channel id from the conversation is passed through.
    kwargs = connect_mock.await_args.kwargs
    assert kwargs["channel_id"] == "chan-abc"
    assert kwargs["tenant_id"] == "tenant_X"
    assert kwargs["external_user_id"] == "u_agent"
    disconnect_mock.assert_awaited_once()


async def test_ws_admin_role_accepted(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    """Admin role can also subscribe (mirrors the REST contract)."""
    conv = _conv()
    fake_ws = _FakeWebSocket()

    async def _ok_decode(_: str) -> dict[str, Any]:
        return {"sub": "u_admin", "tenant_id": "tenant_X", "role": "admin"}

    monkeypatch.setattr(ws_module, "_decode_jwt", _ok_decode)
    monkeypatch.setattr(
        service_module.ConversationService, "get", AsyncMock(return_value=conv)
    )

    connect_mock = AsyncMock(return_value="conn-2")
    disconnect_mock = AsyncMock()
    monkeypatch.setattr(ws_module.manager, "connect", connect_mock)
    monkeypatch.setattr(ws_module.manager, "disconnect", disconnect_mock)

    async def _raise_disconnect() -> str:
        from fastapi import WebSocketDisconnect

        raise WebSocketDisconnect()

    fake_ws.receive_text = _raise_disconnect  # type: ignore[method-assign]

    await ws_module.websocket_endpoint(fake_ws, conv.id, token="jwt")  # noqa: S106

    assert fake_ws.accepted is True
    connect_mock.assert_awaited_once()
