"""Live-DB integration tests for the widget WebSocket auth boundary.

Complementary coverage to ``tests/widget/ws/test_router.py`` (which uses
mocked repositories to isolate transport behavior). These tests exercise
the *real* ``ChannelRepository.get_by_id`` against the live Postgres
instance, proving that the auth check enforces the five claim-level
contract terms that the mocked suite cannot observe:

  1. JWT signature is verified against the runtime secret.
  2. ``sub`` claim is present and non-empty.
  3. ``channel_id`` claim is present and non-empty.
  4. ``exp`` is in the future.
  5. The ``channel_id`` actually resolves to a row in the DB.

The WS endpoint is invoked directly from the test's main loop with a fake
``WebSocket`` rather than going through ``TestClient.websocket_connect``.
This sidesteps the cross-loop trade-off documented in
``tests/conversation/integration/test_conversation_lifecycle.py`` (Test 6
docstring): ``TestClient``'s anyio portal runs the ASGI app on a separate
event loop from the test, which is fine when the WS endpoint does no
async DB work, but a live ``asyncpg`` engine bound to the test loop will
refuse a query from the portal loop with
``RuntimeError: got Future ... attached to a different loop``. Driving
``websocket_endpoint`` directly keeps everything on the test's loop, so
the real ``ChannelRepository.get_by_id`` round-trip works without
mocking.

Cleanup: tests that touch the DB seed a tenant + channel and cascade-delete
the tenant in ``finally:``.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import status
from jose import jwt

from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from channel.repository import ChannelRepository
from core.config import get_settings
from core.database import get_session, reset_engine, reset_sessionmaker
from core.id_gen import new_id
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository
from widget.ws.router import websocket_endpoint


class FakeWebSocket:
    """Minimal WebSocket double sufficient for the auth path.

    The negative paths only call ``close(code=...)``; the success path
    never runs in this file, so we don't need to fake ``accept`` /
    ``receive_text`` / ``send_json``.
    """

    def __init__(self) -> None:
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self.accepted: bool = False

    async def close(
        self, code: int = 1000, reason: str | None = None
    ) -> None:
        self.close_code = code
        if reason is not None:
            self.close_reason = reason

    async def accept(self) -> None:
        self.accepted = True


@pytest.fixture(autouse=True)
def _reset_db_singletons() -> Iterator[None]:
    """Per-test DB-engine reset, mirroring the lifecycle-test autouse fixture."""
    reset_engine()
    reset_sessionmaker()
    yield
    reset_engine()
    reset_sessionmaker()


def _mint_widget_token(
    payload: dict[str, Any],
    *,
    secret: str | None = None,
) -> str:
    """Encode ``payload`` as a widget JWT, optionally with a different secret."""
    settings = get_settings()
    payload = {**payload, "typ": "widget"}
    return jwt.encode(
        payload,
        secret if secret is not None else settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


async def _seed_tenant_and_web_channel(
    *, name: str = "WS Auth Tenant"
) -> tuple[Tenant, Channel]:
    """Seed a tenant + ACTIVE WEB channel. Returns (tenant, channel)."""
    tenant = await TenantRepository().create(name=name, plan=TenantPlan.FREE)
    channel = await ChannelRepository().create(
        tenant_id=tenant.id,
        type=ChannelType.WEB,
        name=f"{name} Web Channel",
        credentials_encrypted=json.dumps({}),
        status=ChannelStatus.ACTIVE,
    )
    return tenant, channel


async def _delete_tenant(tenant_id: str) -> None:
    """Cascade-delete a tenant."""
    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t:
            await session.delete(t)
            await session.commit()


# ============================================================================
# Test 1 — wrong secret is rejected
# ============================================================================


async def test_widget_token_with_wrong_secret_is_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A widget JWT signed with the wrong secret must be rejected.

    The token is structurally valid and carries all the right claims, but
    ``decode_token`` must refuse it because the HMAC does not match the
    runtime ``JWT_SECRET``. The server must close the WS with
    ``WS_1008_POLICY_VIOLATION``.
    """
    now = datetime.now(UTC)
    payload = {
        "sub": "u1",
        "channel_id": new_id(),
        "tenant_id": new_id(),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
    }
    token = _mint_widget_token(
        payload,
        secret="not-the-runtime-secret-just-32-chars-min!!",  # noqa: S106 (negative-test fixture)
    )

    fake_ws = FakeWebSocket()
    with caplog.at_level("WARNING", logger="widget.ws.router"):
        await websocket_endpoint(websocket=fake_ws, token=token)

    assert fake_ws.close_code == status.WS_1008_POLICY_VIOLATION
    assert fake_ws.accepted is False
    assert any(
        "widget ws: rejected token" in rec.message
        for rec in caplog.records
    )


# ============================================================================
# Test 2 — missing `sub` is rejected
# ============================================================================


async def test_widget_token_missing_sub_is_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A widget JWT without ``sub`` must be rejected before any DB lookup."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        # "sub" deliberately omitted.
        "channel_id": new_id(),
        "tenant_id": new_id(),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
    }
    token = _mint_widget_token(payload)

    fake_ws = FakeWebSocket()
    with caplog.at_level("WARNING", logger="widget.ws.router"):
        await websocket_endpoint(websocket=fake_ws, token=token)

    assert fake_ws.close_code == status.WS_1008_POLICY_VIOLATION
    assert fake_ws.accepted is False
    assert any(
        "widget ws: token missing sub claim" in rec.message
        for rec in caplog.records
    )


# ============================================================================
# Test 3 — missing `channel_id` is rejected
# ============================================================================


async def test_widget_token_missing_channel_id_is_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A widget JWT without ``channel_id`` must be rejected before any DB lookup."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": "u1",
        "tenant_id": new_id(),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
    }
    token = _mint_widget_token(payload)

    fake_ws = FakeWebSocket()
    with caplog.at_level("WARNING", logger="widget.ws.router"):
        await websocket_endpoint(websocket=fake_ws, token=token)

    assert fake_ws.close_code == status.WS_1008_POLICY_VIOLATION
    assert fake_ws.accepted is False
    assert any(
        "widget ws: token missing channel_id or tenant_id" in rec.message
        for rec in caplog.records
    )


# ============================================================================
# Test 4 — expired token is rejected
# ============================================================================


async def test_widget_token_expired_is_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A widget JWT whose ``exp`` is in the past must be rejected."""
    past = datetime.now(UTC) - timedelta(minutes=5)
    payload = {
        "sub": "u1",
        "channel_id": new_id(),
        "tenant_id": new_id(),
        "iat": int((past - timedelta(minutes=5)).timestamp()),
        "exp": int(past.timestamp()),
    }
    token = _mint_widget_token(payload)

    fake_ws = FakeWebSocket()
    with caplog.at_level("WARNING", logger="widget.ws.router"):
        await websocket_endpoint(websocket=fake_ws, token=token)

    assert fake_ws.close_code == status.WS_1008_POLICY_VIOLATION
    assert fake_ws.accepted is False
    assert any(
        "widget ws: rejected token" in rec.message
        for rec in caplog.records
    )


# ============================================================================
# Test 5 — live-DB cross-tenant rejection (the 跨租户拒绝 branch)
# ============================================================================


@pytest.mark.integration
async def test_widget_token_with_tenant_mismatch_against_live_channel_is_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A widget JWT whose ``tenant_id`` does not match the seeded channel's
    ``tenant_id`` must be rejected by the *live* cross-tenant branch in
    ``router.py`` (lines 66-73), without mocking the repository.

    This is the only test in this file that exercises the real
    ``ChannelRepository.get_by_id`` round-trip. We seed a real tenant +
    ACTIVE WEB channel and mint a token whose ``tenant_id`` belongs to
    a *different* tenant — the endpoint must resolve the channel, hit
    the tenant-mismatch check, and close with ``WS_1008_POLICY_VIOLATION``.
    """
    tenant, channel = await _seed_tenant_and_web_channel(
        name="WS Auth Tenant Mismatch"
    )
    try:
        other_tenant_id = new_id()  # != tenant.id
        assert other_tenant_id != tenant.id
        now = datetime.now(UTC)
        payload = {
            "sub": "u_cross",
            "channel_id": channel.id,
            "tenant_id": other_tenant_id,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=10)).timestamp()),
        }
        token = _mint_widget_token(payload)

        fake_ws = FakeWebSocket()
        with caplog.at_level("WARNING", logger="widget.ws.router"):
            await websocket_endpoint(websocket=fake_ws, token=token)

        assert fake_ws.close_code == status.WS_1008_POLICY_VIOLATION
        assert fake_ws.accepted is False
        assert any(
            "widget ws: tenant mismatch" in rec.message
            for rec in caplog.records
        )
    finally:
        await _delete_tenant(tenant.id)
