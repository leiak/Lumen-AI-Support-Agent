"""Tests for the shared ``httpx.AsyncClient`` pool (nitpick S4).

Pool reuses a single ``AsyncClient`` per ``(base_url, headers)`` so
HTTPS connection pool (keep-alive + TLS session) is paid once per
provider, not per turn.

The fixture is short-lived: each test creates a fresh pool and
``aclose_all()`` runs in teardown so no real TCP socket survives.
The ``_clients`` attribute is accessed directly because no public
introspection API is justified for a 30-line class.
"""
from __future__ import annotations

import pytest

from llm_client.http_pool import HttpClientPool


@pytest.fixture
async def pool() -> HttpClientPool:
    p = HttpClientPool()
    yield p
    await p.aclose_all()


@pytest.mark.asyncio
async def test_get_or_create_returns_same_client_for_same_key(pool: HttpClientPool) -> None:
    """Same (base_url, headers) returns the same AsyncClient (pool reuse)."""
    headers = {"Authorization": "Bearer x"}
    c1 = await pool.get_or_create(base_url="https://api.example.com", headers=headers)
    c2 = await pool.get_or_create(base_url="https://api.example.com", headers=headers)
    assert c1 is c2


@pytest.mark.asyncio
async def test_get_or_create_returns_different_client_for_different_url(
    pool: HttpClientPool,
) -> None:
    """Different base_url gets a different client (no cross-tenant leakage)."""
    c1 = await pool.get_or_create(base_url="https://api.example.com")
    c2 = await pool.get_or_create(base_url="https://api.other.com")
    assert c1 is not c2


@pytest.mark.asyncio
async def test_aclose_all_drains_pool(pool: HttpClientPool) -> None:
    """aclose_all() closes every cached client."""
    await pool.get_or_create(base_url="https://api.example.com")
    await pool.get_or_create(base_url="https://api.other.com")
    assert len(pool._clients) == 2
    await pool.aclose_all()
    assert len(pool._clients) == 0
