"""Tests for widget WebSocket origin allowlist (Stage 9.10).

Two layers of testing:

1. ``is_origin_allowed`` — pure unit tests on the normalization + comparison
   helper. These don't need any ASGI plumbing; they directly cover the
   cases the integration tests below can't easily reach (missing Origin,
   literal ``*``, case differences).

2. Integration tests via ``TestClient.websocket_connect`` — prove the WS
   router actually reads the ``Origin`` header off the handshake and
   rejects mismatches with ``WS_1008_POLICY_VIOLATION``.

Anti-enumeration is preserved throughout: any rejection (missing,
mismatched, malformed, wildcard) returns the same opaque close code —
nothing in the response surface distinguishes "the allowlist is empty"
from "this origin isn't on the list".
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from channel.repository import ChannelRepository
from core.config import reset_settings
from core.id_gen import new_id
from widget.tokens import create_widget_token
from widget.ws.router import is_origin_allowed, router as ws_router

# Snapshot the real ``is_origin_allowed`` at import time. The widget
# test conftest later replaces it with a permissive test-only helper;
# the integration tests in this module override that with this
# snapshot so the actual rejection logic runs end-to-end.
_REAL_IS_ORIGIN_ALLOWED = is_origin_allowed


@pytest.fixture(autouse=True)
def _restore_real_origin_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the real ``is_origin_allowed`` for every test in this module.

    The shared ``tests/widget/conftest.py`` fixture swaps in a permissive
    test-only helper so the existing widget WS tests aren't broken by
    the new origin check. This module *is* the dedicated origin-test
    suite, so every test here needs the real implementation.
    """
    from widget.ws import router as ws_router_module

    monkeypatch.setattr(
        ws_router_module, "is_origin_allowed", _REAL_IS_ORIGIN_ALLOWED
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the allowlist to a known value for the duration of the test.

    The default in ``Settings`` is fine, but using an explicit list here
    makes assertions about the result unambiguous.
    """
    from core.config import Settings

    monkeypatch.setenv(
        "WIDGET_ALLOWED_ORIGINS_GLOBAL",
        "http://localhost:5173,http://localhost:3000",
    )
    # Force settings to re-read from env.
    reset_settings()
    # Make sure the WS router module's cached `get_settings()` reads the
    # patched env — settings are cached, so we already cleared above; any
    # cached references inside widgets/ws/router would be from import time
    # only if we eagerly read them. We don't, so this single reset is enough.
    yield
    reset_settings()


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
    app.include_router(ws_router)
    return app


# ---------------------------------------------------------------------------
# Pure unit tests on ``is_origin_allowed``
# ---------------------------------------------------------------------------


def test_is_origin_allowed_accepts_listed_origin() -> None:
    """An origin that matches a listed entry (normalized) is allowed."""
    assert is_origin_allowed(
        "http://localhost:5173",
        allowlist=["http://localhost:5173"],
    )


def test_is_origin_allowed_rejects_missing_origin() -> None:
    """No Origin header → reject (default-deny when an allowlist exists)."""
    assert is_origin_allowed(None, allowlist=["http://localhost:5173"]) is False


def test_is_origin_allowed_rejects_wildcard_origin() -> None:
    """A literal ``*`` Origin is never allowed."""
    assert is_origin_allowed("*", allowlist=["http://localhost:5173"]) is False


def test_is_origin_allowed_normalizes_case() -> None:
    """``HTTPS://Example.com`` matches ``https://example.com`` after normalization."""
    assert is_origin_allowed(
        "HTTPS://Example.com",
        allowlist=["https://example.com"],
    )


def test_is_origin_allowed_normalizes_uppercase_scheme_host() -> None:
    """``HTTP://LOCALHOST:5173`` matches the lowercase listed entry."""
    assert is_origin_allowed(
        "HTTP://LOCALHOST:5173",
        allowlist=["http://localhost:5173"],
    )


def test_is_origin_allowed_strips_default_port_http() -> None:
    """``http://localhost:80`` normalizes to ``http://localhost``."""
    assert is_origin_allowed(
        "http://localhost:80",
        allowlist=["http://localhost"],
    )


def test_is_origin_allowed_strips_default_port_https() -> None:
    """``https://example.com:443`` normalizes to ``https://example.com``."""
    assert is_origin_allowed(
        "https://example.com:443",
        allowlist=["https://example.com"],
    )


def test_is_origin_allowed_rejects_unlisted_origin() -> None:
    """An origin not on the allowlist is rejected."""
    assert is_origin_allowed(
        "https://evil.example.com",
        allowlist=["http://localhost:5173"],
    ) is False


# ---------------------------------------------------------------------------
# Integration tests through the WS endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_allowed_origin_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    """An origin on the allowlist completes the WS handshake and the
    frame loop responds to ``ping`` with ``pong``.
    """
    ch = _channel()
    token, _ = create_widget_token(channel=ch, external_user_id="u1")

    async def fake_get_by_id(_self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    testclient = TestClient(_make_app())
    with testclient.websocket_connect(
        f"/api/v1/widget/ws?token={token}",
        headers={"Origin": "http://localhost:5173"},
    ) as ws:
        ws.send_json({"type": "ping"})
        reply = ws.receive_json()
        assert reply == {"type": "pong"}


@pytest.mark.asyncio
async def test_ws_blocked_origin_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An origin NOT on the allowlist causes the WS handshake to fail.

    Anti-enumeration: the close code is the same ``1008`` policy violation
    used for every other rejection reason in this router; the client never
    learns which rule fired.
    """
    ch = _channel()
    token, _ = create_widget_token(channel=ch, external_user_id="u1")

    async def fake_get_by_id(_self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    testclient = TestClient(_make_app())
    with pytest.raises(WebSocketDisconnect):
        with testclient.websocket_connect(
            f"/api/v1/widget/ws?token={token}",
            headers={"Origin": "https://evil.example.com"},
        ) as ws:
            ws.receive_text()


@pytest.mark.asyncio
async def test_ws_missing_origin_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing Origin header is rejected at the WS handshake.

    Starlette's ``TestClient`` does NOT emit an ``Origin`` header on its
    WebSocket handshake (it sets ``host: testserver`` only), so this
    test effectively exercises the "no Origin header" branch of the
    real ``is_origin_allowed`` function via the live WS endpoint.
    """
    ch = _channel()
    token, _ = create_widget_token(channel=ch, external_user_id="u1")

    async def fake_get_by_id(_self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    testclient = TestClient(_make_app())
    with pytest.raises(WebSocketDisconnect):
        with testclient.websocket_connect(
            f"/api/v1/widget/ws?token={token}"
        ) as ws:
            ws.receive_text()


@pytest.mark.asyncio
async def test_ws_wildcard_origin_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Origin: *`` is never on the allowlist → reject."""
    ch = _channel()
    token, _ = create_widget_token(channel=ch, external_user_id="u1")

    async def fake_get_by_id(_self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    testclient = TestClient(_make_app())
    with pytest.raises(WebSocketDisconnect):
        with testclient.websocket_connect(
            f"/api/v1/widget/ws?token={token}",
            headers={"Origin": "*"},
        ) as ws:
            ws.receive_text()


@pytest.mark.asyncio
async def test_ws_case_insensitive_origin_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uppercase scheme/host in the Origin header is accepted after normalization."""
    ch = _channel()
    token, _ = create_widget_token(channel=ch, external_user_id="u1")

    async def fake_get_by_id(_self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    testclient = TestClient(_make_app())
    with testclient.websocket_connect(
        f"/api/v1/widget/ws?token={token}",
        headers={"Origin": "HTTP://Localhost:5173"},
    ) as ws:
        ws.send_json({"type": "ping"})
        reply = ws.receive_json()
        assert reply == {"type": "pong"}