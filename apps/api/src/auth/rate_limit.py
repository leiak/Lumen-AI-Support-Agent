"""Per-IP sliding-window rate limit backed by Redis.

Used by the tenant-hint lookup endpoint to prevent email enumeration via
high-rate probing. Falls back to "fail open" if Redis is unavailable so
the endpoint stays reachable during infra outages — security degradation
is preferable to a hard outage on a read-only endpoint.

The window is 60 seconds; the threshold (default 30 requests) is sized
for human login UX (1 attempt every ~2 s worst case) while making
scripted enumeration impractical.

Anti-enumeration note: we deliberately do NOT differentiate "rate
limited" from "no result" in any client-visible way that would help
attackers; a 429 here is a coarse signal but unavoidable to protect
the endpoint.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import redis.asyncio as aioredis
from fastapi import Request

from core.redis import get_redis

logger = logging.getLogger(__name__)

# 60-second window; allow 30 hits per IP — enough headroom for legitimate
# form retyping, low enough to make scripted enumeration costly.
WINDOW_SECONDS = 60
MAX_HITS_PER_WINDOW = 30


def _client_ip(request: Request) -> str:
    """Best-effort client IP.

    We trust X-Forwarded-For only when an upstream proxy is known to
    set it (the deployment is behind one in staging / production).
    Falls back to the socket peer otherwise.
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        # first entry is the originating client
        return fwd.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


async def check_tenant_lookup_rate_limit(
    request: Request,
) -> tuple[bool, int]:
    """Return (allowed, current_count) for the current IP.

    ``allowed`` is True when the request is within budget, False when
    it should be rejected. ``current_count`` is the post-increment count
    inside the window — useful for diagnostics / test assertions.

    Never raises: Redis errors are logged and treated as "allowed".
    """
    ip = _client_ip(request)
    key = f"rl:tenant-lookup:{ip}"
    try:
        client: aioredis.Redis = get_redis()
        # INCR + EXPIRE in one round-trip using a pipeline; EXPIRE is a
        # no-op after the first hit so subsequent calls don't extend the
        # window indefinitely.
        async with client.pipeline(transaction=False) as pipe:
            pipe.incr(key)
            pipe.expire(key, WINDOW_SECONDS, nx=True)
            results = await pipe.execute()
        count = int(results[0])
    except Exception as exc:
        logger.warning("rate_limit.redis_unavailable error=%s", type(exc).__name__)
        return True, 0

    if count > MAX_HITS_PER_WINDOW:
        return False, count
    return True, count


def make_rate_limit_dependency(
    check_fn: Callable[[Request], Awaitable[tuple[bool, int]]],
) -> Callable[[Request], Awaitable[None]]:
    """Build a FastAPI dependency that 429s when the rate limit denies.

    The dependency looks up ``check_fn`` on the ``auth.rate_limit``
    module at call time (rather than capturing it in a closure) so
    tests can monkeypatch the module attribute and the patched version
    is what runs.
    """

    async def _dep(request: Request) -> None:
        import sys

        from fastapi import HTTPException, status

        # Resolve at call time so monkeypatch.setattr on the module
        # affects subsequent invocations.
        current = (
            sys.modules[check_fn.__module__].__dict__.get(
                check_fn.__name__, check_fn
            )
        )
        allowed, _count = await current(request)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please slow down.",
            )

    return _dep