"""Tests for ``auth.rate_limit._client_ip`` proxy-trust behavior.

The X-Forwarded-For header is only honored when the deployment is behind
a known number of trusted reverse proxies (``TRUSTED_PROXY_HOPS`` > 0).
Direct-internet deployments (the safe default) must ignore the header
entirely so attackers cannot spoof the rate-limit IP.
"""
from __future__ import annotations

import pytest

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