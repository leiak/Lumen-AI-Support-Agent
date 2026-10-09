"""Tests for ``auth.rate_limit._client_ip`` proxy-trust behavior.

The X-Forwarded-For header is only honored when the deployment is behind
a known number of trusted reverse proxies (``TRUSTED_PROXY_HOPS`` > 0).
Direct-internet deployments (the safe default) must ignore the header
entirely so attackers cannot spoof the rate-limit IP.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth import rate_limit
from auth.rate_limit import _client_ip
from core.config import reset_settings


@pytest.fixture
def reset_settings_after_test():
    yield
    reset_settings()


def test_client_ip_uses_socket_when_no_proxy(
    monkeypatch: pytest.MonkeyPatch, reset_settings_after_test
) -> None:
    """With TRUSTED_PROXY_HOPS=0, the X-Forwarded-For header is ignored."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost:5432/z")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", "xK3mF9pL2qR8tN5vW7yA1bC4dE6gH0iJ")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "0")
    reset_settings()
    from fastapi import Request

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"1.2.3.4")],
        "client": ("9.9.9.9", 12345),
    }
    req = Request(scope)
    assert _client_ip(req) == "9.9.9.9"


def test_client_ip_uses_rightmost_n_hops(
    monkeypatch: pytest.MonkeyPatch, reset_settings_after_test
) -> None:
    """With TRUSTED_PROXY_HOPS=2, walk 2 from the right and use the next
    entry leftward as the originating client."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost:5432/z")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", "xK3mF9pL2qR8tN5vW7yA1bC4dE6gH0iJ")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
    reset_settings()
    from fastapi import Request

    # 3 entries: client (1.2.3.4), edge-proxy (10.0.0.1), mid-proxy (10.0.0.2).
    # With trusted_hops=2, the rightmost 2 (10.0.0.1, 10.0.0.2) are the
    # trusted proxy chain, and the next entry leftward (1.2.3.4) is the
    # originating client.
    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"1.2.3.4, 10.0.0.1, 10.0.0.2")],
        "client": ("9.9.9.9", 12345),
    }
    req = Request(scope)
    assert _client_ip(req) == "1.2.3.4"


def test_client_ip_falls_back_to_leftmost_when_header_shorter_than_hops(
    monkeypatch: pytest.MonkeyPatch, reset_settings_after_test
) -> None:
    """When the header has fewer entries than TRUSTED_PROXY_HOPS, fall
    back to the leftmost (most specific) entry — we can't trust the
    proxy chain fully, but the leftmost is still better than the socket
    peer behind a misconfigured proxy."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost:5432/z")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", "xK3mF9pL2qR8tN5vW7yA1bC4dE6gH0iJ")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "3")
    reset_settings()
    from fastapi import Request

    # Header has only 2 entries, but we trust 3 hops. The rightmost 2
    # are proxy chain, but we don't have a 3rd proxy — so we trust the
    # leftmost ("1.2.3.4") as the originating client.
    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"1.2.3.4, 10.0.0.1")],
        "client": ("9.9.9.9", 12345),
    }
    req = Request(scope)
    assert _client_ip(req) == "1.2.3.4"


class _PipelineCtx:
    """Minimal async-context-manager stand-in for a Redis pipeline.

    ``check_tenant_lookup_rate_limit`` uses ``async with client.pipeline(...)``
    and then awaits ``pipe.execute()``. The queued ``incr`` / ``expire`` calls
    are sync (return the pipe to allow chaining) and the failure we want to
    exercise happens at the awaited ``execute()`` — no further wiring needed.
    """

    def __init__(self, pipe: MagicMock) -> None:
        self._pipe = pipe

    async def __aenter__(self) -> MagicMock:
        return self._pipe

    async def __aexit__(self, *args: object) -> None:
        return None


def test_check_tenant_lookup_rate_limit_fails_open_with_metric(
    monkeypatch: pytest.MonkeyPatch, reset_settings_after_test
) -> None:
    """When Redis raises, the check returns (True, 0) AND increments
    the ``rate_limit_fail_open_total`` counter.

    Fails-open must be observable: an alert on the counter lets ops
    notice sustained degradation instead of silently leaving the
    endpoint unprotected.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost:5432/z")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", "xK3mF9pL2qR8tN5vW7yA1bC4dE6gH0iJ")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "0")
    reset_settings()

    from core.business_metrics import RATE_LIMIT_FAIL_OPEN_TOTAL

    before = RATE_LIMIT_FAIL_OPEN_TOTAL.labels(
        check_name="tenant_lookup"
    )._value.get()

    with patch("auth.rate_limit.get_redis") as mock_redis:
        mock_pipe = MagicMock()
        # incr / expire on the real pipeline are sync (they return the pipe
        # to allow chaining) — only ``execute()`` is awaited.
        mock_pipe.incr = MagicMock(return_value=mock_pipe)
        mock_pipe.expire = MagicMock(return_value=mock_pipe)
        mock_pipe.execute = AsyncMock(
            side_effect=ConnectionError("redis down")
        )
        mock_client = MagicMock()
        mock_client.pipeline = MagicMock(
            return_value=_PipelineCtx(mock_pipe)
        )
        mock_redis.return_value = mock_client

        from fastapi import Request

        scope = {
            "type": "http",
            "headers": [],
            "client": ("1.2.3.4", 12345),
        }
        req = Request(scope)
        allowed, count = asyncio.run(
            rate_limit.check_tenant_lookup_rate_limit(req)
        )

    assert allowed is True
    assert count == 0
    after = RATE_LIMIT_FAIL_OPEN_TOTAL.labels(
        check_name="tenant_lookup"
    )._value.get()
    assert after == before + 1
