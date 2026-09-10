"""Tests for widget token HTTP handlers.

Unit-level tests that monkeypatch `ChannelRepository.get_by_id` so they
run without a live DB. Full DB integration tests are deferred to Stage 5
when the seed/test harness matures.
"""
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from channel.repository import ChannelRepository
from core.id_gen import new_id
from widget.api import router as widget_router


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(widget_router)
    return app


def _make_channel(*, status: ChannelStatus = ChannelStatus.ACTIVE) -> Channel:
    return Channel(
        id=new_id(),
        tenant_id=new_id(),
        type=ChannelType.WEB,
        name="x",
        status=status,
        credentials_encrypted="{}",
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_token_endpoint_404_for_unknown_channel(monkeypatch) -> None:
    async def fake_get_by_id(self, _id):
        return None

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/widget/token",
            json={"channel_id": "01HX_UNKNOWN", "external_user_id": "u1"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "channel not found"


@pytest.mark.asyncio
async def test_token_endpoint_403_for_disabled_channel(monkeypatch) -> None:
    disabled = _make_channel(status=ChannelStatus.DISABLED)

    async def fake_get_by_id(self, _id):
        return disabled

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/widget/token",
            json={"channel_id": disabled.id, "external_user_id": "u1"},
        )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "channel is not active"


@pytest.mark.asyncio
async def test_token_endpoint_200_returns_valid_token(monkeypatch) -> None:
    ch = _make_channel()

    async def fake_get_by_id(self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/widget/token",
            json={"channel_id": ch.id, "external_user_id": "u_99"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "token" in body and len(body["token"]) > 20
    assert "expires_at" in body
    assert isinstance(body["expires_in"], int)
    # 30 min TTL -> expires_in should be in (0, 30*60 + a few seconds slack)
    assert 0 < body["expires_in"] <= 30 * 60 + 5


@pytest.mark.asyncio
async def test_refresh_endpoint_401_for_bad_token(monkeypatch) -> None:
    async def fake_get_by_id(self, _id):
        return None

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/widget/token/refresh",
            json={
                "channel_id": "01HX_X",
                "external_user_id": "u1",
                "previous_token": "not.a.real.jwt",
            },
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_endpoint_401_for_channel_mismatch(monkeypatch) -> None:
    """A valid widget token for channel A must not refresh against channel B."""
    from widget.tokens import create_widget_token

    ch_a = _make_channel()
    ch_b_id = new_id()
    token_a, _ = create_widget_token(channel=ch_a, external_user_id="u_1")

    async def fake_get_by_id(self, _id):
        return ch_a

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/widget/token/refresh",
            json={
                "channel_id": ch_b_id,
                "external_user_id": "u_1",
                "previous_token": token_a,
            },
        )
    assert resp.status_code == 401
    assert "channel" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_refresh_endpoint_401_for_user_mismatch(monkeypatch) -> None:
    """A valid widget token for user X must not refresh when the request says user Y."""
    from widget.tokens import create_widget_token

    ch = _make_channel()
    token, _ = create_widget_token(channel=ch, external_user_id="u_real")

    async def fake_get_by_id(self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/widget/token/refresh",
            json={
                "channel_id": ch.id,
                "external_user_id": "u_attacker",
                "previous_token": token,
            },
        )
    assert resp.status_code == 401
    assert "user" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_refresh_endpoint_200_with_valid_previous_token(monkeypatch) -> None:
    from widget.tokens import create_widget_token

    ch = _make_channel()
    prev_token, _ = create_widget_token(channel=ch, external_user_id="u_1")

    async def fake_get_by_id(self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/widget/token/refresh",
            json={
                "channel_id": ch.id,
                "external_user_id": "u_1",
                "previous_token": prev_token,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "token" in body and len(body["token"]) > 20
    assert "expires_at" in body
    assert isinstance(body["expires_in"], int)
    assert 0 < body["expires_in"] <= 30 * 60 + 5
    # verify the new token is itself a valid widget token
    from widget.tokens import decode_widget_token

    decoded = decode_widget_token(body["token"])
    assert decoded["sub"] == "u_1"
    assert decoded["channel_id"] == ch.id
    assert decoded["typ"] == "widget"


@pytest.mark.asyncio
async def test_refresh_rejects_admin_token(monkeypatch) -> None:
    """An admin `access` token (typ != widget) must be rejected by refresh."""
    from auth.jwt import create_access_token

    admin_token = create_access_token(
        tenant_id="t1",
        user_id="u_admin",
        role="admin",
        extra={"typ": "access"},
    )

    async def fake_find_by_id(self, _id):
        return None

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_find_by_id)

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/widget/token/refresh",
            json={
                "channel_id": "01HX_X",
                "external_user_id": "u1",
                "previous_token": admin_token,
            },
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_rejects_expired_token(monkeypatch) -> None:
    """A widget token whose `exp` is in the past must be rejected."""
    from widget.tokens import create_widget_token

    ch = _make_channel()
    expired_token, _ = create_widget_token(
        channel=ch, external_user_id="u_1", ttl_seconds=-60
    )

    async def fake_find_by_id(self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_find_by_id)

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/widget/token/refresh",
            json={
                "channel_id": ch.id,
                "external_user_id": "u_1",
                "previous_token": expired_token,
            },
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_token_endpoint_rejects_empty_channel_id() -> None:
    """Empty channel_id should yield 422 from Pydantic validation."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/widget/token",
            json={"channel_id": "", "external_user_id": "u1"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_refresh_endpoint_rejects_missing_previous_token() -> None:
    """Missing previous_token should yield 422 from Pydantic validation."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/widget/token/refresh",
            json={"channel_id": "01HX_X", "external_user_id": "u1"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_token_endpoint_200_token_decodes_with_widget_claims(monkeypatch) -> None:
    """The issued token must decode as a widget token with correct claims."""
    from widget.tokens import decode_widget_token

    ch = _make_channel()

    async def fake_find_by_id(self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_find_by_id)

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/widget/token",
            json={"channel_id": ch.id, "external_user_id": "u_99"},
        )
    assert resp.status_code == 200
    body = resp.json()
    payload = decode_widget_token(body["token"])
    assert payload["typ"] == "widget"
    assert payload["channel_id"] == ch.id
    assert payload["tenant_id"] == ch.tenant_id
    assert payload["sub"] == "u_99"
