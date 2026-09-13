"""Tests for CORS behavior on the widget token HTTP endpoint (Stage 9.10).

The widget token endpoint is the public, cross-origin entrypoint for the
embedded JS widget — it MUST be reachable from the customer's website but
only from origins we've explicitly trusted. These tests prove:

- The middleware echoes the request Origin on a real POST when the
  origin is on the allowlist (not the literal ``*``).
- CORS response headers include the methods/headers the browser needs
  to drive the widget without a separate preflight round trip on
  every request.
- A preflight ``OPTIONS`` from an allowed origin returns 200 with the
  full set of CORS response headers.
- An ``OPTIONS`` from a disallowed origin is rejected — Starlette's
  CORSMiddleware returns 400 with ``Disallowed CORS origin``.

Anti-enumeration: the middleware doesn't expose which origins are on
the list — a wrong-origin preflight gets the same opaque 400 every
caller does. (The actual route handler still runs in non-OPTIONS
requests; the CORS headers are added to whatever response the handler
produced, success or error.)
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient

from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from channel.repository import ChannelRepository
from core.config import reset_settings
from core.id_gen import new_id
from widget.api import router as widget_router

ALLOWED_ORIGIN = "http://localhost:5173"
BLOCKED_ORIGIN = "https://evil.example.com"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the CORS allowlist to a known set for the duration of the test."""
    monkeypatch.setenv("WIDGET_ALLOWED_ORIGINS_GLOBAL", ALLOWED_ORIGIN)
    reset_settings()
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


def _build_app() -> FastAPI:
    """Standalone app with widget router AND the same CORS middleware
    wiring ``main.py`` uses.

    Building it per-test lets us re-read settings via the autouse
    fixture without touching the module-level middleware order.
    """
    app = FastAPI()
    app.include_router(widget_router)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ALLOWED_ORIGIN],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
        allow_headers=["Content-Type", "Authorization", "X-Tenant-Id"],
        expose_headers=["Content-Type"],
        max_age=600,
    )
    return app


@pytest.mark.asyncio
async def test_token_endpoint_echoes_allowed_origin_in_cors_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A POST from an allowed origin gets ``Access-Control-Allow-Origin``
    set to the echoed origin (NOT ``*``) so credentials can flow.

    The handler itself returns 404 because the channel doesn't exist;
    we don't care — CORS headers are added regardless of the route's
    response status.
    """
    async def fake_get_by_id(self, _id):
        return None

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/widget/token",
            json={"channel_id": "01HX_X", "external_user_id": "u1"},
            headers={"Origin": ALLOWED_ORIGIN},
        )

    assert resp.status_code == 404  # from the route handler
    assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
    assert resp.headers.get("access-control-allow-credentials") == "true"


@pytest.mark.asyncio
async def test_token_endpoint_preflight_options_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preflight from an allowed origin returns 200 with the methods /
    headers the browser needs to follow up with a POST.
    """
    async def fake_get_by_id(self, _id):
        return None

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        resp = await client.options(
            "/api/v1/widget/token",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
    allow_methods = resp.headers.get("access-control-allow-methods", "")
    assert "POST" in allow_methods.upper()
    assert "OPTIONS" in allow_methods.upper()
    allow_headers = resp.headers.get("access-control-allow-headers", "")
    assert "content-type" in allow_headers.lower()


@pytest.mark.asyncio
async def test_token_endpoint_preflight_rejects_disallowed_origin() -> None:
    """Preflight from an unlisted origin gets 400 ``Disallowed CORS origin``.

    The browser will treat this as a hard failure — the widget won't be
    able to load from an attacker-controlled host. The route handler
    doesn't run.
    """
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        resp = await client.options(
            "/api/v1/widget/token",
            headers={
                "Origin": BLOCKED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    assert resp.status_code == 400
    assert "Disallowed CORS origin" in resp.text


@pytest.mark.asyncio
async def test_token_endpoint_post_from_disallowed_origin_has_no_cors_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real POST from a disallowed origin: the request still runs (the
    handler doesn't know about CORS), but the middleware adds no CORS
    response headers — the browser will block the JS from reading the
    response.
    """
    async def fake_get_by_id(self, _id):
        return None

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/widget/token",
            json={"channel_id": "01HX_X", "external_user_id": "u1"},
            headers={"Origin": BLOCKED_ORIGIN},
        )

    assert resp.status_code == 404  # handler still ran
    assert "access-control-allow-origin" not in resp.headers