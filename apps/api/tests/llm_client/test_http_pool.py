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


@pytest.mark.asyncio
async def test_aclose_all_tolerates_one_failing_client(
    pool: HttpClientPool, capsys: pytest.CaptureFixture[str]
) -> None:
    """aclose_all() uses ``gather(..., return_exceptions=True)`` (bd54df5) so
    one client failing to close doesn't leak the others' TCP sockets.

    Pinning the defensive teardown contract: even if one client's
    ``aclose()`` raises (broken socket, closed loop, whatever), the
    others still close. We assert both:
      * The failing client's exception is captured (no re-raise).
      * A structured warning is rendered so an operator can grep for
        it post-mortem.

    Note on capture: ``_log`` is a structlog ``BoundLogger`` (see
    :mod:`core.logging`). structlog's default ``PrintLoggerFactory``
    writes to ``stdout``/``stderr``, not the stdlib ``logging`` module,
    so ``caplog`` won't catch the records — we read it back via
    ``capsys``.
    """
    healthy = await pool.get_or_create(base_url="https://api.example.com")
    broken = await pool.get_or_create(base_url="https://api.other.com")

    boom = RuntimeError("simulated close failure")
    # Monkey-patch the broken client's aclose to raise. Real httpx
    # never raises on close under normal circumstances — this fakes
    # the "closed loop" / "broken socket" environment we want the
    # defensive gather() to survive.
    async def _raise_aclose() -> None:
        raise boom

    broken.aclose = _raise_aclose  # type: ignore[method-assign]

    # Healthy client's aclose is real — confirm it was actually
    # closed at the end (we check via the internal flag httpx sets).
    # Must not raise.
    await pool.aclose_all()
    captured = capsys.readouterr()

    # The pool itself is drained regardless of the failure.
    assert len(pool._clients) == 0

    # A warning fired with the exception's class name. Look for the
    # event key + the error type name. structlog's renderer is set by
    # ``core.logging.configure_logging()`` and is JSON in production
    # (so the field is ``"error_type": "RuntimeError"``) but key=value
    # in isolation (when configure_logging hasn't been called yet).
    # Accept both formats.
    combined = captured.out + captured.err
    assert "llm_client.http_pool.aclose_failed" in combined, (
        f"expected aclose_failed warning, got stdout={captured.out!r} "
        f"stderr={captured.err!r}"
    )
    assert "error_type" in combined and "RuntimeError" in combined, (
        f"expected error_type=RuntimeError (or JSON equivalent), "
        f"got {combined!r}"
    )

    # The healthy client was actually closed (httpx sets is_closed).
    assert healthy.is_closed is True
    # And the gather returned the exception rather than re-raising it.
    # (We can't inspect results directly because we already returned —
    # the fact that we got here without an exception is the assertion.)
    assert boom is not None  # sentinel — the original boom was captured, not raised
