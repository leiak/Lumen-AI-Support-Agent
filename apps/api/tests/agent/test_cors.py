"""Tests for CORS behavior on the agent / auth HTTP endpoints (Stage 9.10).

The agent workspace SPA is browser-based and lives on a different origin
from the API in development (Vite on ``http://localhost:5173`` proxies to
``http://localhost:8000``). These tests prove the CORS middleware:

- Echoes the request Origin for an allowed origin (NOT ``*`` so that
  credentials — the JWT in the Authorization header — can be carried).
- Sets ``Access-Control-Allow-Credentials: true``.
- Accepts preflight ``OPTIONS`` from an allowed origin and returns the
  full set of CORS headers the browser needs.

We deliberately use lightweight endpoints that don't require a DB:
``/api/v1/auth/lookup-tenant`` returns 200 with the anti-enumeration
shape regardless of whether the user exists, and ``/api/v1/agents/me``
returns 401 without a Bearer token — both let us assert CORS headers
without exercising any business logic.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient

from agent import api as agent_api_module
from auth.api import router as auth_router

ALLOWED_ORIGIN = "http://localhost:5173"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import reset_settings

    monkeypatch.setenv("WIDGET_ALLOWED_ORIGINS_GLOBAL", ALLOWED_ORIGIN)
    reset_settings()
    yield
    reset_settings()


def _build_app() -> FastAPI:
    """Standalone app with the agent + auth routers + CORS middleware.

    Mirrors what ``main.py`` does in production: CORSMiddleware with
    credentials enabled and a known allowlist.
    """
    app = FastAPI()
    app.include_router(agent_api_module.router)
    app.include_router(auth_router)
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
async def test_auth_login_response_echoes_allowed_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``POST /api/v1/auth/login`` from an allowed origin returns
    ``Access-Control-Allow-Origin: <origin>`` plus
    ``Access-Control-Allow-Credentials: true`` so the browser allows the
    SPA to read the JWT response.

    We don't exercise real auth — we stub ``AuthService.login`` to
    raise ``InvalidCredentialsError`` (401) so we don't need a DB.
    """

    async def fake_login(*_args: Any, **_kwargs: Any) -> None:
        from auth.service import InvalidCredentialsError

        raise InvalidCredentialsError("bad credentials")

    monkeypatch.setattr("auth.api.AuthService.login", fake_login)

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "user@example.com", "password": "longenough-password"},
            headers={
                "Origin": ALLOWED_ORIGIN,
                "X-Tenant-Id": "t1",
            },
        )

    # 401 from the stubbed auth, but CORS headers must still be present.
    assert resp.status_code == 401
    assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
    assert resp.headers.get("access-control-allow-credentials") == "true"


@pytest.mark.asyncio
async def test_agents_me_response_echoes_allowed_origin() -> None:
    """``GET /api/v1/agents/me`` without a Bearer token returns 401, but
    the CORS headers must still be on the response so the SPA can see
    the failure (otherwise the browser would hide the response).
    """
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/agents/me",
            headers={"Origin": ALLOWED_ORIGIN},
        )

    assert resp.status_code == 401
    assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
    assert resp.headers.get("access-control-allow-credentials") == "true"


@pytest.mark.asyncio
async def test_preflight_options_returns_200_for_allowed_origin() -> None:
    """Preflight from the agent SPA's origin → 200 with method/header
    allow-list. The browser will only fire the real request after this
    preflight succeeds.
    """
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://test"
    ) as client:
        resp = await client.options(
            "/api/v1/agents/me",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
    methods = resp.headers.get("access-control-allow-methods", "").upper()
    assert "GET" in methods
    assert "OPTIONS" in methods
    headers = resp.headers.get("access-control-allow-headers", "").lower()
    assert "authorization" in headers