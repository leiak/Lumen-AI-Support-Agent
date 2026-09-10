"""Tests for the unified channel management API.

Pure-Python tests — no DB needed. We monkeypatch the auth dependency and
the `ChannelService` methods so the route handlers run against in-memory
fakes. This keeps the suite fast and side-effect-free.

Note on monkeypatching: `ChannelService.<method>` is replaced on the
class. When Python then looks up `service.method`, the descriptor
protocol binds the *instance* automatically — so our fakes must accept
`self` as their first parameter, just like a real method.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from channel import api as api_module
from channel import service as service_module
from channel.api import router as channels_router
from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from core.id_gen import new_id

# A JWT-shaped claims dict for an admin of tenant_X.
ADMIN_CLAIMS: dict[str, Any] = {
    "sub": "u_admin",
    "tenant_id": "tenant_X",
    "role": "admin",
}


async def _fake_require_admin() -> dict[str, Any]:
    """Bypass JWT verification — directly inject admin claims."""
    return ADMIN_CLAIMS


def _channel(**kwargs: Any) -> Channel:
    """Build a Channel ORM instance with sensible defaults."""
    base: dict[str, Any] = dict(
        id=new_id(),
        tenant_id=ADMIN_CLAIMS["tenant_id"],
        type=ChannelType.WEB,
        name="default",
        status=ChannelStatus.ACTIVE,
        credentials_encrypted="{}",
        created_at=datetime.now(UTC),
    )
    base.update(kwargs)
    return Channel(**base)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(channels_router)
    return app


def _stub_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the route dependency with one that returns admin claims."""
    monkeypatch.setattr(api_module, "require_admin", _fake_require_admin)


# ---------------------------------------------------------------------------
# create_channel
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_channel_returns_201_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST should persist and return metadata only — no credentials in response."""
    _stub_auth(monkeypatch)

    captured: dict[str, Any] = {}

    async def fake_create(
        self: Any,
        *,
        tenant_id: str,
        type: ChannelType,
        name: str,
        credentials: dict[str, Any],
    ) -> Channel:
        captured["tenant_id"] = tenant_id
        captured["type"] = type
        captured["name"] = name
        captured["credentials"] = credentials
        return _channel(name=name, type=type)

    monkeypatch.setattr(service_module.ChannelService, "create", fake_create)

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/channels",
            json={
                "type": "feishu",
                "name": "My Feishu Bot",
                "credentials": {"app_id": "cli_x", "app_secret": "secret"},
            },
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "My Feishu Bot"
    assert body["type"] == "feishu"
    assert body["tenant_id"] == "tenant_X"
    assert body["status"] == "active"
    # The whole point: credentials MUST NOT appear in the response.
    assert "credentials" not in body
    assert "credentials_encrypted" not in body

    # And the service layer received the credentials verbatim from the request.
    assert captured["tenant_id"] == "tenant_X"
    assert captured["credentials"] == {"app_id": "cli_x", "app_secret": "secret"}


# ---------------------------------------------------------------------------
# list_channels
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_channels_returns_only_tenant_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET should return all (and only) channels belonging to the caller's tenant."""
    _stub_auth(monkeypatch)

    channels = [
        _channel(name="c1"),
        _channel(name="c2", type=ChannelType.EMAIL),
    ]

    async def fake_list(self: Any, *, tenant_id: str) -> list[Channel]:
        assert tenant_id == "tenant_X"
        return channels

    monkeypatch.setattr(service_module.ChannelService, "list", fake_list)

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/channels")

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 2
    names = {ch["name"] for ch in body}
    assert names == {"c1", "c2"}
    # No credential leakage on list either.
    for ch in body:
        assert "credentials" not in ch
        assert "credentials_encrypted" not in ch


# ---------------------------------------------------------------------------
# get_channel
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_channel_404_for_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_auth(monkeypatch)

    async def fake_get(self: Any, *, tenant_id: str, channel_id: str) -> Channel | None:
        return None

    monkeypatch.setattr(service_module.ChannelService, "get", fake_get)

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/channels/01HX_UNKNOWN")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "channel not found"


@pytest.mark.asyncio
async def test_get_channel_returns_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_auth(monkeypatch)
    ch = _channel(name="widget", type=ChannelType.WEB)

    async def fake_get(self: Any, *, tenant_id: str, channel_id: str) -> Channel | None:
        return ch

    monkeypatch.setattr(service_module.ChannelService, "get", fake_get)

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/v1/channels/{ch.id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == ch.id
    assert body["name"] == "widget"
    assert body["type"] == "web"
    assert "credentials" not in body


# ---------------------------------------------------------------------------
# patch_channel
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_patch_channel_updates_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """PATCH should forward all provided fields to the service."""
    _stub_auth(monkeypatch)

    captured: dict[str, Any] = {}

    async def fake_update(
        self: Any,
        *,
        tenant_id: str,
        channel_id: str,
        name: str | None = None,
        status: ChannelStatus | None = None,
        credentials: dict[str, Any] | None = None,
    ) -> Channel | None:
        captured["tenant_id"] = tenant_id
        captured["channel_id"] = channel_id
        captured["name"] = name
        captured["status"] = status
        captured["credentials"] = credentials
        return _channel(name=name or "x", status=status or ChannelStatus.ACTIVE)

    monkeypatch.setattr(service_module.ChannelService, "update", fake_update)

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch(
            "/api/v1/channels/01HX_X",
            json={"name": "renamed", "status": "disabled"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "disabled"
    assert resp.json()["name"] == "renamed"
    assert captured["tenant_id"] == "tenant_X"
    assert captured["channel_id"] == "01HX_X"
    assert captured["name"] == "renamed"
    assert captured["status"] == ChannelStatus.DISABLED
    # Credentials omitted from body => forwarded as None.
    assert captured["credentials"] is None
    assert "credentials" not in resp.json()


@pytest.mark.asyncio
async def test_patch_channel_404_for_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_auth(monkeypatch)

    async def fake_update(
        self: Any,
        *,
        tenant_id: str,
        channel_id: str,
        name: str | None = None,
        status: ChannelStatus | None = None,
        credentials: dict[str, Any] | None = None,
    ) -> Channel | None:
        return None

    monkeypatch.setattr(service_module.ChannelService, "update", fake_update)

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch("/api/v1/channels/01HX_X", json={"name": "x"})

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_channel_credentials_replaces_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credentials dict in PATCH should be passed through to the service."""
    _stub_auth(monkeypatch)

    captured: dict[str, Any] = {}

    async def fake_update(
        self: Any,
        *,
        tenant_id: str,
        channel_id: str,
        name: str | None = None,
        status: ChannelStatus | None = None,
        credentials: dict[str, Any] | None = None,
    ) -> Channel | None:
        captured["credentials"] = credentials
        return _channel()

    monkeypatch.setattr(service_module.ChannelService, "update", fake_update)

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch(
            "/api/v1/channels/01HX_X",
            json={"credentials": {"app_id": "cli_new", "app_secret": "new_secret"}},
        )

    assert resp.status_code == 200
    assert captured["credentials"] == {"app_id": "cli_new", "app_secret": "new_secret"}
    # Echoed back as part of a ChannelOut — still no credentials field.
    assert "credentials" not in resp.json()


# ---------------------------------------------------------------------------
# delete_channel
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_delete_channel_soft_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_auth(monkeypatch)

    captured: dict[str, Any] = {}

    async def fake_soft_delete(
        self: Any, *, tenant_id: str, channel_id: str
    ) -> Channel | None:
        captured["tenant_id"] = tenant_id
        captured["channel_id"] = channel_id
        return _channel(status=ChannelStatus.DISABLED)

    monkeypatch.setattr(service_module.ChannelService, "soft_delete", fake_soft_delete)

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete("/api/v1/channels/01HX_X")

    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"
    assert captured["tenant_id"] == "tenant_X"
    assert captured["channel_id"] == "01HX_X"


@pytest.mark.asyncio
async def test_delete_channel_404_for_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_auth(monkeypatch)

    async def fake_soft_delete(
        self: Any, *, tenant_id: str, channel_id: str
    ) -> Channel | None:
        return None

    monkeypatch.setattr(service_module.ChannelService, "soft_delete", fake_soft_delete)

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete("/api/v1/channels/01HX_X")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Authorization (admin role gate)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_admin_role_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-admin role must be rejected with 403 before any service call."""

    async def fake_require_admin() -> dict[str, Any]:
        raise HTTPException(status_code=403, detail="admin role required")

    monkeypatch.setattr(api_module, "require_admin", fake_require_admin)

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/channels")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "admin role required"


@pytest.mark.asyncio
async def test_missing_bearer_returns_401() -> None:
    """No Authorization header at all -> 401."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/channels")

    assert resp.status_code == 401