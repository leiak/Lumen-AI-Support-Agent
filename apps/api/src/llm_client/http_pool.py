"""Shared httpx.AsyncClient pool.

Caches one ``AsyncClient`` per ``(base_url, frozenset(headers.items()))``
so the HTTPS connection pool (keep-alive + TLS session) is reused across
turns. Replaces per-turn construction in
``llm_client.providers.anthropic_provider.AnthropicProvider`` /
``...openai_provider.OpenAIProvider`` (nitpick S4 / tech-debt #24).

A single process-wide instance lives in ``agent.llm_factory._http_pool``;
``main.py`` lifespan calls :func:`agent.llm_factory.aclose_http_pool` on
shutdown so the underlying TCP sockets are released. Tests construct
short-lived instances via the fixture in
``tests/llm_client/test_http_pool.py``.

Loop binding: ``httpx.AsyncClient`` lazily binds to the running event
loop on first request. We construct inside :meth:`get_or_create` so the
first caller's loop wins; subsequent callers in the same loop reuse the
bound client. Between tests the conftest's ``_reset_singletons`` fixture
drops the module-level pool reference so the next test creates a fresh
client on its own loop.
"""
from __future__ import annotations

import httpx


class HttpClientPool:
    """Process-local pool of ``httpx.AsyncClient`` keyed by ``(base_url, headers)``.

    The pool is intentionally tiny — typically one entry per active
    provider (one for Anthropic, one for OpenAI/OpenAI-compatible,
    plus per-tenant variants for BYOK keys since headers differ).
    Bounded by the cardinality of unique ``(base_url, headers)``
    combinations actually requested, so an LRU is not warranted here.

    Thread/loop safety: callers MUST share an event loop for reuse;
    constructing the client is sync (no I/O), but the underlying
    connection pool binds on first request. Cross-loop reuse is not
    supported; the test isolation fixture clears the pool between
    event-loop-scoped tests.
    """

    def __init__(self) -> None:
        self._clients: dict[tuple[str, frozenset[tuple[str, str]]], httpx.AsyncClient] = {}

    async def get_or_create(
        self,
        *,
        base_url: str,
        headers: dict[str, str] | None = None,
        client_timeout: httpx.Timeout | None = None,
    ) -> httpx.AsyncClient:
        """Return the cached client for ``(base_url, headers)`` or create one.

        Key shape: ``(base_url, frozenset(headers.items()))``. A ``frozenset``
        (not ``tuple``) makes the key hash-order-independent so callers
        that build the dict in different orders still hit the same
        cached entry.

        ``client_timeout`` is named to avoid the ASYNC109 false positive
        against ``asyncio.timeout`` (the param has nothing to do with
        ``asyncio.timeout`` — it forwards straight to ``httpx.Timeout``).
        """
        key = (base_url, frozenset((headers or {}).items()))
        client = self._clients.get(key)
        if client is not None:
            return client
        client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers or {},
            timeout=client_timeout or httpx.Timeout(30.0),
        )
        self._clients[key] = client
        return client

    async def aclose_all(self) -> None:
        """Close every cached client. Idempotent; safe to call twice.

        Drain order: snapshot the values first so a closed client
        cannot be returned to a concurrent caller between calls.
        """
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            await client.aclose()


__all__ = ["HttpClientPool"]
