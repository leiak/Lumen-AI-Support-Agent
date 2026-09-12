"""Tests for GET /api/v1/auth/lookup-tenant.

Anti-enumeration contract: every test that calls the endpoint MUST
verify the response is always HTTP 200 with the same JSON shape
regardless of whether the email is registered.
"""
from __future__ import annotations

import asyncio
from typing import ClassVar

from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.api import router as auth_router
from auth.password import hash_password
from auth.rate_limit import check_tenant_lookup_rate_limit
from core.database import get_session, reset_engine, reset_sessionmaker
from core.id_gen import new_id
from tenant.enums import TenantPlan, TenantStatus, UserRole
from tenant.models import Tenant
from tenant.repository import UserRepository

# Build a minimal FastAPI app that mounts ONLY the auth router. We avoid
# the full ``main.app`` because it pulls in other routers (knowledge,
# conversation, etc.) that have their own transitive import costs and
# FastAPI 0.115+ 204-with-None-return validation problems unrelated to
# this endpoint.
app = FastAPI()
app.include_router(auth_router)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_single_user_sync() -> tuple[str, str]:
    """Seed one tenant + one user on a fresh event loop.

    Returns ``(tenant_id, email)``. We deliberately open our own event
    loop and discard it before the TestClient runs (which uses its own
    loop). The autouse ``_reset_singletons`` fixture clears the engine
    between tests so the loop boundary is safe.
    """
    tenant_id = new_id()
    email = f"alice-{tenant_id}@acme.com"

    async def _go() -> None:
        async with get_session() as session:
            t = Tenant(
                id=tenant_id,
                name=f"Acme-{tenant_id[:8]}",
                plan=TenantPlan.PRO,
                status=TenantStatus.ACTIVE,
            )
            session.add(t)
            await session.flush()
            await session.commit()
        await UserRepository().create(
            tenant_id=tenant_id,
            email=email,
            password_hash=hash_password("password123"),
            role=UserRole.ADMIN,
            full_name="Alice",
        )

    asyncio.run(_go())
    reset_engine()
    reset_sessionmaker()
    return tenant_id, email


def _seed_multi_tenant_same_email_sync() -> tuple[str, str, str, str]:
    """Seed two tenants each with a user sharing the same email.

    Returns (lower_id_tenant, higher_id_tenant, shared_email,
    first_user_id). The "lower id" tenant is the one whose user sorts
    first by ULID for the deterministic tiebreaker test.
    """
    tid_a = new_id()
    tid_b = new_id()
    if tid_a > tid_b:
        tid_a, tid_b = tid_b, tid_a
    shared_email = f"shared-{tid_a}-{tid_b}@multi.com"

    async def _go() -> str:
        for tid in (tid_a, tid_b):
            async with get_session() as session:
                t = Tenant(
                    id=tid,
                    name=f"Tenant-{tid[:6]}",
                    plan=TenantPlan.PRO,
                    status=TenantStatus.ACTIVE,
                )
                session.add(t)
                await session.flush()
                await session.commit()
        first = await UserRepository().create(
            tenant_id=tid_a,
            email=shared_email,
            password_hash=hash_password("pw-a"),
            role=UserRole.AGENT,
            full_name="First",
        )
        await UserRepository().create(
            tenant_id=tid_b,
            email=shared_email,
            password_hash=hash_password("pw-b"),
            role=UserRole.AGENT,
            full_name="Second",
        )
        return first.id

    first_id = asyncio.run(_go())
    reset_engine()
    reset_sessionmaker()
    return tid_a, tid_b, shared_email, first_id


def seeded_single_user() -> tuple[str, str]:
    return _seed_single_user_sync()


def seeded_multi_tenant_same_email() -> tuple[str, str, str, str]:
    return _seed_multi_tenant_same_email_sync()


def _disable_rate_limit(monkeypatch) -> None:
    """Bypass the per-IP rate limiter in every test here.

    Individual tests can re-enable / monkeypatch it explicitly when they
    want to exercise the limiter itself.

    Patches BOTH the re-exported reference in ``auth.api`` AND the
    original ``auth.rate_limit.check_tenant_lookup_rate_limit`` so the
    module-level dependency wiring picks up the stub.
    """
    async def _always_allowed(_request):
        return True, 0

    monkeypatch.setattr(
        "auth.rate_limit.check_tenant_lookup_rate_limit", _always_allowed
    )
    monkeypatch.setattr(
        "auth.api.check_tenant_lookup_rate_limit", _always_allowed
    )


# ---------------------------------------------------------------------------
# Lookup tests
# ---------------------------------------------------------------------------


def test_lookup_tenant_returns_tenant_for_existing_user(monkeypatch) -> None:
    _disable_rate_limit(monkeypatch)
    tenant_id, email = seeded_single_user()
    client = TestClient(app)

    resp = client.get("/api/v1/auth/lookup-tenant", params={"email": email})

    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == tenant_id
    assert body["tenant_name"] == f"Acme-{tenant_id[:8]}"


def test_lookup_tenant_returns_nulls_for_unknown_email(monkeypatch) -> None:
    _disable_rate_limit(monkeypatch)
    seeded_single_user()
    client = TestClient(app)

    resp = client.get(
        "/api/v1/auth/lookup-tenant",
        params={"email": "definitely-not-registered@nowhere.example.com"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"tenant_id": None, "tenant_name": None}


def test_lookup_tenant_response_is_identical_shape_for_hit_and_miss(
    monkeypatch,
) -> None:
    """Anti-enumeration: same status + same keys for both branches.

    Both requests share one ``TestClient`` (preferred over two — two
    clients back-to-back on Windows triggers an unrelated httpx /
    asyncpg pool teardown race). The two requests hit the same handler
    so the only state the assertion cares about is the response shape,
    which is invariant across the hit/miss branches.
    """
    _disable_rate_limit(monkeypatch)
    _tenant_id, email = seeded_single_user()

    with TestClient(app) as client:
        hit_resp = client.get(
            "/api/v1/auth/lookup-tenant", params={"email": email}
        )
        miss_resp = client.get(
            "/api/v1/auth/lookup-tenant",
            params={"email": "no-such-user@example.com"},
        )

    assert hit_resp.status_code == miss_resp.status_code == 200
    assert set(hit_resp.json().keys()) == set(miss_resp.json().keys())
    assert set(hit_resp.json().keys()) == {"tenant_id", "tenant_name"}


def test_lookup_tenant_does_not_log_email(monkeypatch, caplog) -> None:
    """PII: the email must NEVER appear in any log record.

    structlog routes through stdlib logging when configured, so we scan
    caplog for the raw email literal. The assertion is intentionally
    brittle: a single leak fails the test.
    """
    import logging

    _disable_rate_limit(monkeypatch)
    _tenant_id, email = seeded_single_user()
    client = TestClient(app)

    caplog.set_level(logging.DEBUG)
    resp = client.get("/api/v1/auth/lookup-tenant", params={"email": email})
    assert resp.status_code == 200

    leaked = [r for r in caplog.records if email in r.getMessage()]
    assert leaked == [], "email leaked into a log record"


def test_lookup_tenant_invalidates_malformed_email(monkeypatch) -> None:
    """422 from FastAPI for missing @ — anti-enumeration does NOT mean we
    have to accept obviously broken input; this is just normal input
    validation, not a lookup success/failure distinction."""
    _disable_rate_limit(monkeypatch)
    client = TestClient(app)
    resp = client.get(
        "/api/v1/auth/lookup-tenant", params={"email": "no-at-sign"}
    )
    assert resp.status_code == 422


def test_lookup_tenant_handles_multi_tenant_users_with_same_email(
    monkeypatch,
) -> None:
    """Two users with the same email → pick the one with the lowest id."""
    _disable_rate_limit(monkeypatch)
    lower_id, _higher_id, email, _first_id = seeded_multi_tenant_same_email()
    client = TestClient(app)

    resp = client.get("/api/v1/auth/lookup-tenant", params={"email": email})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == lower_id


def test_lookup_tenant_rate_limit_triggers_after_threshold(
    monkeypatch,
) -> None:
    """When the rate limiter denies, the endpoint must 429 — not 200
    with a fake success, which would leak that we know the IP is hostile.
    """
    async def _always_denied(_request):
        return False, 99

    # The dependency looks up ``check_tenant_lookup_rate_limit`` on
    # ``auth.rate_limit`` at call time, so we must patch the source
    # module rather than the re-export in ``auth.api``.
    monkeypatch.setattr(
        "auth.rate_limit.check_tenant_lookup_rate_limit", _always_denied
    )
    monkeypatch.setattr(
        "auth.api.check_tenant_lookup_rate_limit", _always_denied
    )
    client = TestClient(app)
    resp = client.get(
        "/api/v1/auth/lookup-tenant", params={"email": "anything@example.com"}
    )
    assert resp.status_code == 429


def test_lookup_tenant_inactive_user_returns_nulls(monkeypatch) -> None:
    """An inactive user must NOT resolve to a tenant — the lookup is for
    active, login-eligible accounts only."""
    from sqlalchemy import update

    from tenant.models import User

    _disable_rate_limit(monkeypatch)
    _tenant_id, email = seeded_single_user()

    async def _deactivate() -> None:
        async with get_session() as session:
            await session.execute(
                update(User).where(User.email == email).values(is_active=False)
            )
            await session.commit()

    asyncio.run(_deactivate())
    reset_engine()
    reset_sessionmaker()

    client = TestClient(app)
    resp = client.get("/api/v1/auth/lookup-tenant", params={"email": email})
    assert resp.status_code == 200
    assert resp.json() == {"tenant_id": None, "tenant_name": None}


# ---------------------------------------------------------------------------
# Rate-limit helper unit tests (no DB needed)
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal stand-in for ``fastapi.Request`` — only the attributes the
    rate-limit helper reads."""

    class _Client:
        host = "127.0.0.1"

    client = _Client()
    headers: ClassVar[dict[str, str]] = {}


def test_check_tenant_lookup_rate_limit_helper_signature(monkeypatch) -> None:
    """Smoke test that the helper returns the documented tuple shape.

    Avoids a real Redis call by patching the global client with a stub.
    """

    class _StubPipeline:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def incr(self, _key):
            return self

        def expire(self, _key, _seconds, nx=False):
            return self

        async def execute(self):
            return [1, True]

    class _StubRedis:
        def pipeline(self, transaction=False):
            return _StubPipeline()

    from core import redis as redis_module

    old = redis_module._client
    redis_module._client = _StubRedis()  # type: ignore[assignment]
    try:
        result = asyncio.run(
            check_tenant_lookup_rate_limit(_FakeRequest())
        )
        allowed, count = result
        assert allowed is True
        assert count == 1
    finally:
        redis_module._client = old  # type: ignore[assignment]