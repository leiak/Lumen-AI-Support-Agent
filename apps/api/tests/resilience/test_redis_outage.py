"""Resilience test: Redis is down during a tenant-lookup rate-limit check.

Contract pinned by this test (see :func:`auth.rate_limit.check_tenant_lookup_rate_limit`):

1. When Redis raises (any exception, including ``ConnectionError``), the
   check returns ``(True, 0)`` — the request is **allowed** and the
   customer-facing endpoint stays reachable.
2. The failure is observable: the ``rate_limit_fail_open_total{check_name}``
   counter increments by exactly one so an ops alert can fire on sustained
   degradation.

This is the "graceful degradation" invariant. A regression that swallows
Redis errors without incrementing the counter would silently re-enable
unbounded enumeration; a regression that raised on Redis errors would 5xx
the login endpoint during a Redis outage. Both regressions are caught here.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request

from auth import rate_limit
from core.business_metrics import RATE_LIMIT_FAIL_OPEN_TOTAL


class _RaisingPipelineCtx:
    """Async-context-manager stand-in for a Redis pipeline that explodes on execute.

    The real pipeline is used as ``async with client.pipeline(...)`` and then
    awaited with ``pipe.execute()``. ``incr`` / ``expire`` are sync (return the
    pipe for chaining); only ``execute()`` is awaited. We only need to crash at
    the await boundary to exercise the fail-open path.
    """

    def __init__(self, pipe: MagicMock) -> None:
        self._pipe = pipe

    async def __aenter__(self) -> MagicMock:
        return self._pipe

    async def __aexit__(self, *args: object) -> None:
        return None


@asynccontextmanager
async def _fake_redis_client(
    *, exc: BaseException | None = None,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[None]:
    """Patch ``auth.rate_limit.get_redis`` to return a client whose
    pipeline raises ``exc`` on ``execute()``.

    The real ``get_redis()`` returns a singleton — patches here are local
    to the context and the singleton is untouched so other tests keep
    working. Defaults to ``ConnectionError("redis down")`` to mirror the
    most common outage shape.
    """
    if exc is None:
        exc = ConnectionError("redis down")
    mock_pipe = MagicMock()
    mock_pipe.incr = MagicMock(return_value=mock_pipe)
    mock_pipe.expire = MagicMock(return_value=mock_pipe)
    mock_pipe.execute = AsyncMock(side_effect=exc)

    mock_client = MagicMock()
    mock_client.pipeline = MagicMock(return_value=_RaisingPipelineCtx(mock_pipe))

    monkeypatch.setattr(rate_limit, "get_redis", lambda: mock_client)
    yield


def _make_request() -> Request:
    """Build a minimal FastAPI ``Request`` with a socket peer IP.

    The IP resolver (``_client_ip``) reads ``request.client.host`` when
    no X-Forwarded-For is trusted — a present ``client`` is enough.
    """
    scope = {
        "type": "http",
        "headers": [],
        "client": ("1.2.3.4", 12345),
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_redis_outage_fails_open_and_increments_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: Redis pipeline raises ConnectionError → (True, 0) +
    ``rate_limit_fail_open_total{check_name="tenant_lookup"}`` += 1.
    """
    before = RATE_LIMIT_FAIL_OPEN_TOTAL.labels(
        check_name="tenant_lookup"
    )._value.get()

    async with _fake_redis_client(monkeypatch=monkeypatch):
        allowed, count = await rate_limit.check_tenant_lookup_rate_limit(
            _make_request()
        )

    assert allowed is True, "Redis outage must fail open (allow the request)"
    assert count == 0, "Failed-open path returns count=0 (no INCR result)"

    after = RATE_LIMIT_FAIL_OPEN_TOTAL.labels(
        check_name="tenant_lookup"
    )._value.get()
    assert after == before + 1, (
        "Fail-open MUST be observable via the counter so ops can alert on it"
    )


@pytest.mark.asyncio
async def test_redis_outage_with_timeout_still_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Variant: a timeout (``asyncio.TimeoutError``) is treated identically.

    The except clause in :func:`check_tenant_lookup_rate_limit` catches
    ``Exception`` so any backend error — DNS, timeout, refused
    connection, auth — degrades the same way. Pin that here so a future
    narrowing of the catch doesn't accidentally 500 the endpoint on a
    transient timeout.
    """

    before = RATE_LIMIT_FAIL_OPEN_TOTAL.labels(
        check_name="tenant_lookup"
    )._value.get()

    async with _fake_redis_client(
        exc=TimeoutError(), monkeypatch=monkeypatch
    ):
        allowed, count = await rate_limit.check_tenant_lookup_rate_limit(
            _make_request()
        )

    assert allowed is True
    assert count == 0
    after = RATE_LIMIT_FAIL_OPEN_TOTAL.labels(
        check_name="tenant_lookup"
    )._value.get()
    assert after == before + 1


__all__ = [
    "test_redis_outage_fails_open_and_increments_counter",
    "test_redis_outage_with_timeout_still_fails_open",
]
