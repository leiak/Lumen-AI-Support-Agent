"""Live-DB integration tests for the widget WebSocket auth boundary.

Complementary coverage to ``tests/widget/ws/test_router.py`` (which uses
mocked repositories to isolate transport behavior). These tests exercise
the *real* ``ChannelRepository.get_by_id`` against the live Postgres
instance, proving that the auth check enforces the four claim-level
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
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import status
from jose import jwt

from channel.enums import ChannelStatus, ChannelType
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
def _reset_db_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
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
) -> tuple[Tenant, object]:
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


@pytest.mark.integration
async def test_widget_token_with_wrong_secret_is_rejected() -> None:
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
    await websocket_endpoint(websocket=fake_ws, token=token)

    assert fake_ws.close_code == status.WS_1008_POLICY_VIOLATION
    assert fake_ws.accepted is False


# ============================================================================
# Test 2 — missing `sub` is rejected
# ============================================================================


@pytest.mark.integration
async def test_widget_token_missing_sub_is_rejected() -> None:
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
    await websocket_endpoint(websocket=fake_ws, token=token)

    assert fake_ws.close_code == status.WS_1008_POLICY_VIOLATION
    assert fake_ws.accepted is False


# ============================================================================
# Test 3 — missing `channel_id` is rejected
# ============================================================================


@pytest.mark.integration
async def test_widget_token_missing_channel_id_is_rejected() -> None:
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
    await websocket_endpoint(websocket=fake_ws, token=token)

    assert fake_ws.close_code == status.WS_1008_POLICY_VIOLATION
    assert fake_ws.accepted is False


# ============================================================================
# Test 4 — expired token is rejected
# ============================================================================


@pytest.mark.integration
async def test_widget_token_expired_is_rejected() -> None:
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
    await websocket_endpoint(websocket=fake_ws, token=token)

    assert fake_ws.close_code == status.WS_1008_POLICY_VIOLATION
    assert fake_ws.accepted is False


# ============================================================================
# Test 5 — token for unknown channel is rejected (live DB lookup)
# ============================================================================


@pytest.mark.integration
async def test_widget_token_for_unknown_channel_is_rejected() -> None:
    """A structurally-valid widget JWT for a channel that does not exist
    in the DB must be rejected, not silently accepted.

    This is the *only* test in this file that exercises the live
    ``ChannelRepository.get_by_id`` path: it seeds a real tenant + channel
    so the engine is wired up and we have something to cascade-delete, but
    the token targets a different ``channel_id`` that is guaranteed not
    to be present in the channels table.
    """
    tenant, _seeded_channel = await _seed_tenant_and_web_channel(
        name="WS Auth Unknown Channel Tenant"
    )
    try:
        # Generate a channel_id that we never insert into the DB.
        unknown_channel_id = new_id()
        now = datetime.now(UTC)
        payload = {
            "sub": "u_unknown",
            "channel_id": unknown_channel_id,
            "tenant_id": tenant.id,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=10)).timestamp()),
        }
        token = _mint_widget_token(payload)

        fake_ws = FakeWebSocket()
        await websocket_endpoint(websocket=fake_ws, token=token)

        # The endpoint runs ``repo.get_by_id(unknown_channel_id)`` against
        # the real DB; it returns None, so the server closes the WS.
        assert fake_ws.close_code == status.WS_1008_POLICY_VIOLATION
        assert fake_ws.accepted is False

        # Sanity: the unknown_channel_id really is not in the DB.
        repo = ChannelRepository()
        looked_up = await repo.get_by_id(unknown_channel_id)
        assert looked_up is None
    finally:
        await _delete_tenant(tenant.id)
