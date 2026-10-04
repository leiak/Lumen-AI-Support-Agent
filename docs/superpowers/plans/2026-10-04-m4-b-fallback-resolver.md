# M4.B FallbackResolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a chain-based fallback resolver that lets a primary LLM provider fail over to a secondary on transient errors (5xx, 429, timeout, malformed response), layered on the M4.A resolver seam.

**Architecture:** New `FallbackResolver` class in `llm_client/resolvers.py` wraps a list of `PinnedResolver` instances and exposes an `ainvoke()` method that iterates the chain, catching 4 transient exception types. `LLMClient.chat()` detects this via `hasattr(resolver, "ainvoke")` and routes to the new path. When all steps fail, raises `FallbackChainExhausted(ProviderUnavailable)` carrying `attempts: list[AttemptRecord]`. The `Resolver = Callable[[ChatRequest], BaseProvider]` protocol stays unchanged so M4.A's 46 tests stay green.

**Tech Stack:** Python 3.11 / asyncio / prometheus_client / pytest + pytest-asyncio + pytest-httpx

**Spec:** [`docs/superpowers/specs/2026-10-04-m4-b-fallback-resolver-design.md`](../specs/2026-10-04-m4-b-fallback-resolver-design.md)
**Predecessor:** M4.A shipped at commit `f1a63ae`. M4.A's resolver seam is the foundation.

---

## File Structure

| Path | Responsibility |
|------|----------------|
| `apps/api/src/llm_client/exceptions.py` | Add `FallbackChainExhausted(ProviderUnavailable)` + `AttemptRecord(NamedTuple)` |
| `apps/api/src/llm_client/resolvers.py` | Add `FallbackResolver` class + `_FALLBACK_TRIGGERS` tuple |
| `apps/api/src/llm_client/gateway.py` | Accept `default_fallback_chain`; build `FallbackResolver` when set |
| `apps/api/src/llm_client/provider_registry.py` | Add `parse_fallback_chain_env()` parser |
| `apps/api/src/llm_client/client.py` | `chat()` ainvoke branch; `max_retries` default 3 → 1; new `ROUTE_FALLBACK` |
| `apps/api/src/llm_client/__init__.py` | Export new public symbols |
| `apps/api/src/agent/llm_factory.py` | Read `LLM_FALLBACK_CHAIN` env, pass to `LLMGateway` |
| `apps/api/src/core/config.py` | Add `llm_fallback_chain` + `llm_fallback_attempt_timeout_s` settings |
| `apps/api/src/core/business_metrics.py` | Add `LLM_FALLBACK_ATTEMPTS_TOTAL` Counter |
| `apps/api/tests/llm_client/test_fallback_resolver.py` | 10 unit tests for `FallbackResolver` |
| `apps/api/tests/llm_client/test_client_fallback_branch.py` | 5 LLMClient ainvoke branch tests |
| `apps/api/tests/llm_client/integration/test_fallback_e2e.py` | 4 e2e tests via `pytest-httpx` |
| `apps/api/tests/llm_client/test_gateway_fallback.py` | 3 LLMGateway constructor + parser tests |
| `README.md` | M4.B status row + 4 known tech debt items |

No DB migration. No frontend change. No new top-level module.

---

## Task 1: FallbackResolver class + exceptions + business metric + first unit tests

**Files:**
- Modify: `apps/api/src/llm_client/exceptions.py:1-40`
- Modify: `apps/api/src/llm_client/resolvers.py:1-114`
- Modify: `apps/api/src/core/business_metrics.py:60-119`
- Create: `apps/api/tests/llm_client/test_fallback_resolver.py`

This task builds the resolver in isolation. Tests use stub providers only (no httpx, no settings). Pure unit tests against the class.

- [ ] **Step 1.1: Write the failing tests**

Create `apps/api/tests/llm_client/test_fallback_resolver.py`:

```python
"""Unit tests for llm_client.resolvers.FallbackResolver."""
from __future__ import annotations

import asyncio
from unittest.mock import create_autospec

import pytest

from llm_client.exceptions import (
    FallbackChainExhausted,
    InvalidRequest,
    OutputInvalid,
    ProviderUnavailable,
    RateLimited,
)
from llm_client.providers.base import BaseProvider
from llm_client.resolvers import FallbackResolver, PinnedResolver
from llm_client.types import ChatMessage, ChatRequest, ChatResponse, MessageRole


def _stub_provider(name: str) -> BaseProvider:
    """isinstance-compatible BaseProvider stub with a settable chat side_effect."""
    p = create_autospec(BaseProvider, instance=True)
    p.name = name
    return p


def _request(model: str = "anything") -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )


def _response(provider_name: str, model: str) -> ChatResponse:
    return ChatResponse(
        content="ok",
        model=model,
        prompt_tokens=5,
        completion_tokens=3,
        finish_reason="stop",
        provider_name=provider_name,
    )


def _step(provider: BaseProvider, model: str) -> PinnedResolver:
    return PinnedResolver(provider=provider, model=model)


# ---- constructor validation ----


def test_constructor_rejects_empty_steps() -> None:
    with pytest.raises(ValueError, match="at least one step"):
        FallbackResolver(steps=[])


def test_constructor_rejects_duplicate_steps() -> None:
    p = _stub_provider("minimax")
    with pytest.raises(ValueError, match="duplicate"):
        FallbackResolver(steps=[_step(p, "MiniMax-M3"), _step(p, "MiniMax-M3")])


# ---- happy path ----


def test_first_step_success_returns_immediately() -> None:
    primary = _stub_provider("minimax")
    backup = _stub_provider("anthropic")
    primary.chat.return_value = _response("minimax", "MiniMax-M3")
    r = FallbackResolver(steps=[_step(primary, "MiniMax-M3"), _step(backup, "claude-haiku-4-5")])

    resp = asyncio.run(r.ainvoke(_request()))

    assert resp.model == "MiniMax-M3"
    primary.chat.assert_awaited_once()
    backup.chat.assert_not_called()


# ---- trigger exceptions ----


def test_provider_unavailable_triggers_fallback() -> None:
    primary = _stub_provider("minimax")
    backup = _stub_provider("anthropic")
    primary.chat.side_effect = ProviderUnavailable("minimax 503")
    backup.chat.return_value = _response("anthropic", "claude-haiku-4-5")
    r = FallbackResolver(steps=[_step(primary, "MiniMax-M3"), _step(backup, "claude-haiku-4-5")])

    resp = asyncio.run(r.ainvoke(_request()))

    assert resp.provider_name == "anthropic"
    primary.chat.assert_awaited_once()
    backup.chat.assert_awaited_once()


def test_output_invalid_triggers_fallback() -> None:
    primary = _stub_provider("minimax")
    backup = _stub_provider("anthropic")
    primary.chat.side_effect = OutputInvalid("malformed JSON")
    backup.chat.return_value = _response("anthropic", "claude-haiku-4-5")
    r = FallbackResolver(steps=[_step(primary, "MiniMax-M3"), _step(backup, "claude-haiku-4-5")])

    resp = asyncio.run(r.ainvoke(_request()))

    assert resp.provider_name == "anthropic"


def test_rate_limited_triggers_fallback() -> None:
    primary = _stub_provider("minimax")
    backup = _stub_provider("anthropic")
    primary.chat.side_effect = RateLimited("429")
    backup.chat.return_value = _response("anthropic", "claude-haiku-4-5")
    r = FallbackResolver(steps=[_step(primary, "MiniMax-M3"), _step(backup, "claude-haiku-4-5")])

    resp = asyncio.run(r.ainvoke(_request()))

    assert resp.provider_name == "anthropic"


def test_timeout_triggers_fallback() -> None:
    primary = _stub_provider("minimax")
    backup = _stub_provider("anthropic")

    async def slow(*args, **kwargs):
        await asyncio.sleep(5)
        return _response("minimax", "MiniMax-M3")

    primary.chat.side_effect = slow
    backup.chat.return_value = _response("anthropic", "claude-haiku-4-5")
    r = FallbackResolver(
        steps=[_step(primary, "MiniMax-M3"), _step(backup, "claude-haiku-4-5")],
        attempt_timeout_s=0.05,
    )

    resp = asyncio.run(r.ainvoke(_request()))

    assert resp.provider_name == "anthropic"


# ---- non-trigger exception ----


def test_invalid_request_does_NOT_trigger_fallback() -> None:
    primary = _stub_provider("minimax")
    backup = _stub_provider("anthropic")
    primary.chat.side_effect = InvalidRequest("bad schema")
    r = FallbackResolver(steps=[_step(primary, "MiniMax-M3"), _step(backup, "claude-haiku-4-5")])

    with pytest.raises(InvalidRequest, match="bad schema"):
        asyncio.run(r.ainvoke(_request()))

    backup.chat.assert_not_called()


# ---- chain exhaustion ----


def test_all_steps_fail_raises_chain_exhausted() -> None:
    primary = _stub_provider("minimax")
    backup = _stub_provider("anthropic")
    primary.chat.side_effect = ProviderUnavailable("minimax 503")
    backup.chat.side_effect = RateLimited("anthropic 429")
    r = FallbackResolver(steps=[_step(primary, "MiniMax-M3"), _step(backup, "claude-haiku-4-5")])

    with pytest.raises(FallbackChainExhausted) as excinfo:
        asyncio.run(r.ainvoke(_request()))

    attempts = excinfo.value.attempts
    assert len(attempts) == 2
    assert attempts[0].provider_name == "minimax"
    assert attempts[0].model == "MiniMax-M3"
    assert attempts[0].exc_type == "ProviderUnavailable"
    assert attempts[1].provider_name == "anthropic"
    assert attempts[1].model == "claude-haiku-4-5"
    assert attempts[1].exc_type == "RateLimited"


def test_chain_exhausted_is_provider_unavailable() -> None:
    assert issubclass(FallbackChainExhausted, ProviderUnavailable)
```

- [ ] **Step 1.2: Run tests to verify they fail**

```bash
cd apps/api && python -m pytest tests/llm_client/test_fallback_resolver.py -v
```

Expected: import error or `AttributeError: module 'llm_client.resolvers' has no attribute 'FallbackResolver'`. No tests pass.

- [ ] **Step 1.3: Add `FallbackChainExhausted` + `AttemptRecord` to exceptions**

Append to `apps/api/src/llm_client/exceptions.py` (after the existing `OutputInvalid`):

```python
from typing import NamedTuple


class AttemptRecord(NamedTuple):
    """Single attempt result inside a fallback chain.

    Carries the provider name + model + exception class name (string) so
    ops can see which steps were tried and why they failed. PII-safe:
    no message content, no request bodies — just identifiers + error class.

    Attributes:
        provider_name: gateway-registered provider name (e.g. "minimax").
        model: model name actually used for this attempt (may differ from
            request.model when steps override).
        exc_type: ``type(exc).__name__`` of the exception that aborted
            this step (e.g. "ProviderUnavailable", "RateLimited").
    """

    provider_name: str
    model: str
    exc_type: str


class FallbackChainExhausted(ProviderUnavailable):
    """All steps in a :class:`FallbackResolver` chain failed.

    Subclasses :class:`ProviderUnavailable` so callers that already
    handle that exception (e.g. an outer retry loop) treat chain
    exhaustion as a retryable infrastructure failure.

    Attributes:
        attempts: ordered list of every attempted step with its
            exception class. Useful for ops triage (``llm_client.
            fallback_chain_exhausted`` log line) and unit tests
            that want to assert on the attempt history.
    """

    def __init__(self, *, attempts: list[AttemptRecord]) -> None:
        self.attempts = list(attempts)
        providers = ", ".join(f"{a.provider_name}:{a.model}" for a in attempts)
        super().__init__(
            f"Fallback chain exhausted after {len(attempts)} attempt(s): "
            f"[{providers}]"
        )
```

- [ ] **Step 1.4: Add `FallbackResolver` + `_FALLBACK_TRIGGERS` to resolvers**

Append to `apps/api/src/llm_client/resolvers.py` (before the `__all__`):

```python
import asyncio
from typing import TYPE_CHECKING

from llm_client.exceptions import (
    AttemptRecord,
    FallbackChainExhausted,
    InvalidRequest,
    OutputInvalid,
    ProviderUnavailable,
    RateLimited,
)

if TYPE_CHECKING:
    from llm_client.types import ChatResponse


# Exception types that trigger fallback to the next step in the chain.
# ``InvalidRequest`` is intentionally absent: 4xx-equivalent errors are
# caller mistakes, not transient. Fallback would mask the bug and waste
# tokens on the secondary provider.
_FALLBACK_TRIGGERS: tuple[type[BaseException], ...] = (
    ProviderUnavailable,
    OutputInvalid,
    RateLimited,
    asyncio.TimeoutError,
)


class FallbackResolver:
    """Chain-based fallback resolver: try each step in order, return first success.

    Each step is a :class:`PinnedResolver` carrying a ``(provider, model)``
    pair. The chain is iterated on transient failures (see
    :data:`_FALLBACK_TRIGGERS`); on the first successful step a
    :class:`ChatResponse` is returned and on exhaustion
    :class:`FallbackChainExhausted` is raised.

    The :meth:`__call__` shim returns the **primary step's provider** so
    the class satisfies ``Resolver = Callable[[ChatRequest], BaseProvider]``
    (M4.A contract unchanged). Real chain execution goes through
    :meth:`ainvoke`; ``LLMClient.chat`` detects the resolver via
    ``hasattr(resolver, "ainvoke")`` and routes there.
    """

    def __init__(
        self,
        *,
        steps: list["PinnedResolver"],
        attempt_timeout_s: float | None = None,
    ) -> None:
        if not steps:
            raise ValueError("FallbackResolver needs at least one step")
        # Reject duplicate (provider.name, model) pairs to keep metric
        # labels unambiguous and prevent operator typos from silently
        # shadowing each other.
        seen: set[tuple[str, str]] = set()
        for step in steps:
            key = (step.provider.name, step.model)
            if key in seen:
                raise ValueError(
                    f"duplicate step {key!r} in fallback chain"
                )
            seen.add(key)
        self.steps = list(steps)
        self._timeout = attempt_timeout_s

    def __call__(self, request: "ChatRequest") -> "BaseProvider":
        """Return primary step's provider (M4.A protocol compatibility).

        This is a placeholder for callers that only need to know which
        provider to use (e.g. ``stream_chat``). Real chain execution
        goes through :meth:`ainvoke`.
        """
        return self.steps[0].provider

    async def ainvoke(self, request: "ChatRequest") -> "ChatResponse":
        """Execute the chain; return first successful response.

        On transient failure of any step, record the attempt and move to
        the next. ``InvalidRequest`` (and any non-trigger exception)
        propagates immediately — these are caller errors, not transient
        infrastructure problems.

        Returns:
            The :class:`ChatResponse` from the first successful step.

        Raises:
            FallbackChainExhausted: every step raised a trigger exception.
            InvalidRequest: a step raised ``InvalidRequest`` (not retried,
                surfaced immediately).
            Exception: any non-trigger, non-``InvalidRequest`` exception
                propagates (config bug / runtime crash — not a fallback
                candidate).
        """
        attempts: list[AttemptRecord] = []
        last_exc: BaseException | None = None
        for step in self.steps:
            rewritten = request.model_copy(update={"model": step.model})
            try:
                if self._timeout is not None:
                    resp = await asyncio.wait_for(
                        step.provider.chat(rewritten),
                        timeout=self._timeout,
                    )
                else:
                    resp = await step.provider.chat(rewritten)
                return resp
            except _FALLBACK_TRIGGERS as exc:
                attempts.append(
                    AttemptRecord(
                        provider_name=step.provider.name,
                        model=step.model,
                        exc_type=type(exc).__name__,
                    )
                )
                last_exc = exc
                continue
        # All steps exhausted.
        assert last_exc is not None  # invariant: N>=1 steps always set this
        raise FallbackChainExhausted(attempts=attempts) from last_exc
```

Update the module `__all__` to include the new public symbol:

```python
__all__ = ["FallbackResolver", "PinnedResolver", "Resolver", "UnknownModelError"]
```

- [ ] **Step 1.5: Run tests to verify they pass**

```bash
cd apps/api && python -m pytest tests/llm_client/test_fallback_resolver.py -v
```

Expected: 10 tests pass.

- [ ] **Step 1.6: Commit**

```bash
cd apps/api && git add src/llm_client/exceptions.py src/llm_client/resolvers.py tests/llm_client/test_fallback_resolver.py
git commit -m "feat(llm-client): FallbackResolver + FallbackChainExhausted (M4.B Task 1)

Adds the core chain-based fallback class and the aggregated failure
exception. Pure unit tests with stub providers — no httpx, no settings.

- FallbackResolver: list[PinnedResolver] + attempt_timeout_s, exposes
  ainvoke() to iterate the chain on 4 trigger exception types
- _FALLBACK_TRIGGERS = (ProviderUnavailable, OutputInvalid, RateLimited,
  asyncio.TimeoutError); InvalidRequest is intentionally excluded
- FallbackChainExhausted(ProviderUnavailable) carries attempts:
  list[AttemptRecord] for ops triage
- __call__() shim returns primary step's provider so the M4.A Resolver
  protocol (Callable[[ChatRequest], BaseProvider]) stays unchanged
- 10 unit tests cover: ctor validation, all 4 trigger paths, InvalidRequest
  propagation, chain exhaustion with attempt history, subclass check
- Spec: docs/superpowers/specs/2026-10-04-m4-b-fallback-resolver-design.md
- Predecessor: M4.A commit f1a63ae

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 2: LLMClient ainvoke branch + max_retries default

**Files:**
- Modify: `apps/api/src/llm_client/client.py:35-152`
- Create: `apps/api/tests/llm_client/test_client_fallback_branch.py`

Wires `FallbackResolver` into `LLMClient.chat()`. Two changes: new `ROUTE_FALLBACK` route_mode label, and a `hasattr(resolver, "ainvoke")` branch that bypasses the existing retry loop. Also reduces default `max_retries` from 3 to 1 for non-fallback paths.

- [ ] **Step 2.1: Write the failing tests**

Create `apps/api/tests/llm_client/test_client_fallback_branch.py`:

```python
"""Unit tests for LLMClient ainvoke branch + max_retries default."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from llm_client.client import LLMClient, ROUTE_FALLBACK
from llm_client.exceptions import (
    FallbackChainExhausted,
    InvalidRequest,
    ProviderUnavailable,
)
from llm_client.providers.base import BaseProvider
from llm_client.resolvers import FallbackResolver, PinnedResolver
from llm_client.types import ChatMessage, ChatRequest, ChatResponse, MessageRole


def _stub_provider(name: str) -> BaseProvider:
    p = create_autospec(BaseProvider, instance=True)
    p.name = name
    return p


def _request() -> ChatRequest:
    return ChatRequest(
        model="MiniMax-M3",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )


def _response(provider_name: str, model: str) -> ChatResponse:
    return ChatResponse(
        content="ok",
        model=model,
        prompt_tokens=5,
        completion_tokens=3,
        finish_reason="stop",
        provider_name=provider_name,
    )


# ---- route_mode label ----


def test_route_mode_for_fallback_resolver() -> None:
    p = _stub_provider("minimax")
    r = FallbackResolver(steps=[PinnedResolver(provider=p, model="MiniMax-M3")])
    client = LLMClient(provider_resolver=r, tenant_id="t1")
    assert client._route_mode == ROUTE_FALLBACK


# ---- ainvoke branch ----


def test_chat_uses_ainvoke_when_resolver_exposes_it() -> None:
    primary = _stub_provider("minimax")
    ainvoke_mock = AsyncMock(return_value=_response("minimax", "MiniMax-M3"))
    resolver = MagicMock()
    resolver.__class__ = MagicMock  # ensure hasattr check sees ainvoke
    # Build a FallbackResolver and patch its ainvoke to verify delegation.
    real = FallbackResolver(steps=[PinnedResolver(provider=primary, model="MiniMax-M3")])
    real.ainvoke = ainvoke_mock  # type: ignore[method-assign]
    client = LLMClient(provider_resolver=real, tenant_id="t1")

    resp = asyncio.run(client.chat(_request()))

    ainvoke_mock.assert_awaited_once()
    assert resp.provider_name == "minimax"
    # Primary provider.chat was NOT called directly — the chain ran via ainvoke.
    primary.chat.assert_not_called()


def test_chat_propagates_chain_exhausted_without_internal_retry() -> None:
    primary = _stub_provider("minimax")
    real = FallbackResolver(steps=[PinnedResolver(provider=primary, model="MiniMax-M3")])
    real.ainvoke = AsyncMock(  # type: ignore[method-assign]
        side_effect=FallbackChainExhausted(attempts=[])
    )
    client = LLMClient(provider_resolver=real, tenant_id="t1")

    with pytest.raises(FallbackChainExhausted):
        asyncio.run(client.chat(_request()))


def test_chat_does_not_retry_chain_exhausted_even_with_max_retries_5() -> None:
    """LLMClient.chat's retry loop must NOT run when the resolver has ainvoke.

    Otherwise max_retries=5 × chain_length=2 = up to 10 attempts.
    """
    primary = _stub_provider("minimax")
    real = FallbackResolver(steps=[PinnedResolver(provider=primary, model="MiniMax-M3")])
    call_count = 0

    async def fake_ainvoke(req):
        nonlocal call_count
        call_count += 1
        raise FallbackChainExhausted(attempts=[])

    real.ainvoke = fake_ainvoke  # type: ignore[method-assign]
    client = LLMClient(provider_resolver=real, tenant_id="t1")

    with pytest.raises(FallbackChainExhausted):
        asyncio.run(client.chat(_request(), max_retries=5))

    assert call_count == 1, f"expected 1 ainvoke call, got {call_count}"


# ---- max_retries default ----


def test_default_max_retries_is_one_for_non_fallback_resolver() -> None:
    """PinnedResolver / PrefixResolver path now defaults to 1 (was 3)."""
    p = _stub_provider("minimax")
    p.chat.side_effect = ProviderUnavailable("503")
    pinned = PinnedResolver(provider=p, model="MiniMax-M3")
    client = LLMClient(provider_resolver=pinned, tenant_id="t1")

    with pytest.raises(ProviderUnavailable):
        asyncio.run(client.chat(_request()))

    # max_retries=1 → exactly 2 attempts (initial + 1 retry).
    assert p.chat.await_count == 2, f"expected 2 attempts, got {p.chat.await_count}"
```

- [ ] **Step 2.2: Run tests to verify the new ones fail**

```bash
cd apps/api && python -m pytest tests/llm_client/test_client_fallback_branch.py -v
```

Expected: 5 tests fail (no `ROUTE_FALLBACK` export, no ainvoke branch, default still 3).

- [ ] **Step 2.3: Update client.py — `ROUTE_FALLBACK` + `_route_mode_for`**

In `apps/api/src/llm_client/client.py`:

1. Import `FallbackResolver` near the existing resolver import (line 31):

```python
from llm_client.resolvers import (
    FallbackResolver,
    PinnedResolver,
    Resolver,
    UnknownModelError,
)
```

2. Add `ROUTE_FALLBACK` next to the other route mode constants (line 37–40):

```python
ROUTE_AUTO = "auto"
ROUTE_FALLBACK = "fallback"  # NEW — M4.B FallbackResolver chain
ROUTE_PINNED = "pinned"
ROUTE_UNKNOWN_MODEL = "unknown_model"
ROUTE_RESOLVER_ERROR = "resolver_error"
```

3. Extend `_route_mode_for` (line 47–57):

```python
def _route_mode_for(resolver: Resolver) -> str:
    """Pick the ``route_mode`` metric label value for a given resolver.

    Order matters: ``FallbackResolver`` check must precede the
    ``PinnedResolver`` check because a FallbackResolver's outer
    ``__call__`` returns the primary step's provider (a PinnedResolver
    in spirit) but its ``route_mode`` semantically means "a chain ran".
    """
    if isinstance(resolver, FallbackResolver):
        return ROUTE_FALLBACK
    if isinstance(resolver, PinnedResolver):
        return ROUTE_PINNED
    return ROUTE_AUTO
```

- [ ] **Step 2.4: Add ainvoke branch + reduce default max_retries**

In `apps/api/src/llm_client/client.py`, update the `chat` method signature (line 207–212) and prepend the ainvoke branch BEFORE the retry loop.

Find this block:

```python
async def chat(
    self,
    request: ChatRequest,
    *,
    max_retries: int = 3,
) -> ChatResponse:
    """Send a chat request with retry.

    Retries on ProviderUnavailable / OutputInvalid up to `max_retries`
    times with exponential backoff + jitter. Does NOT retry on
    RateLimited / InvalidRequest / UnknownModelError / resolver error
    (those are caller's mistake or hard 4xx-equivalent failures).
    """
    request, route_mode = self._resolve_request(request)
```

Replace `max_retries: int = 3` with `max_retries: int = 1` and insert the branch BEFORE the existing `_resolve_request` line:

```python
async def chat(
    self,
    request: ChatRequest,
    *,
    max_retries: int = 1,
) -> ChatResponse:
    """Send a chat request with retry.

    Retries on ProviderUnavailable / OutputInvalid up to `max_retries`
    times with exponential backoff + jitter. Does NOT retry on
    RateLimited / InvalidRequest / UnknownModelError / resolver error
    (those are caller's mistake or hard 4xx-equivalent failures).

    When ``provider_resolver`` exposes an ``ainvoke`` method
    (FallbackResolver from M4.B), the chain IS the retry mechanism —
    we delegate and skip the local retry loop to avoid
    ``max_retries × chain_length`` attempt explosion.
    """
    # M4.B: FallbackResolver.ainvoke runs the whole chain (primary +
    # backup, ...) and either returns success or raises
    # FallbackChainExhausted. We deliberately do NOT wrap it in this
    # method's retry loop — the chain already handles per-step retry
    # semantics, and re-running it on exhaustion would compound.
    if hasattr(self.provider_resolver, "ainvoke"):
        return await self.provider_resolver.ainvoke(request)

    request, route_mode = self._resolve_request(request)
```

- [ ] **Step 2.5: Run tests to verify they pass**

```bash
cd apps/api && python -m pytest tests/llm_client/test_client_fallback_branch.py tests/llm_client/test_fallback_resolver.py -v
```

Expected: 15 tests pass (10 resolver + 5 client branch).

- [ ] **Step 2.6: Run existing client + resolver + gateway tests — confirm no regressions**

```bash
cd apps/api && python -m pytest tests/llm_client/test_client.py tests/llm_client/test_client_stream.py tests/llm_client/test_resolvers.py tests/llm_client/test_gateway.py -v
```

Expected: all pre-existing tests pass. `test_max_retries_default` style tests (if any) may need their default updated from 3 to 1; investigate any failure before proceeding.

- [ ] **Step 2.7: Commit**

```bash
cd apps/api && git add src/llm_client/client.py tests/llm_client/test_client_fallback_branch.py
git commit -m "feat(llm-client): LLMClient ainvoke branch + max_retries default 1 (M4.B Task 2)

Wires FallbackResolver.ainvoke into LLMClient.chat via hasattr check
that bypasses the existing retry loop. Chain handles per-step retry
semantics itself; re-running it on exhaustion would compound.

- ROUTE_FALLBACK = 'fallback' added to route_mode label set
- _route_mode_for checks FallbackResolver BEFORE PinnedResolver (chain
  outer wrapper semantic)
- chat() default max_retries: 3 -> 1 (chain IS the retry for fallback
  path; bare PinnedResolver/PrefixResolver path now makes one attempt
  instead of three, matching chain-fallback reliability)
- 5 new unit tests in test_client_fallback_branch.py:
  * route_mode label = 'fallback' for FallbackResolver
  * chat() delegates to resolver.ainvoke (provider.chat not called direct)
  * FallbackChainExhausted propagates without internal retry
  * max_retries=5 still produces exactly 1 ainvoke call (no compound)
  * PinnedResolver path with default max_retries=1 makes 2 attempts

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 3: Config + parser + LLMGateway default_fallback_chain

**Files:**
- Modify: `apps/api/src/core/config.py:108-107` (append after `git_sha` field)
- Modify: `apps/api/src/llm_client/provider_registry.py:1-57` (append parser)
- Modify: `apps/api/src/llm_client/gateway.py:38-124`
- Modify: `apps/api/src/llm_client/__init__.py`
- Create: `apps/api/tests/llm_client/test_gateway_fallback.py`

Wires the chain from env → Settings → LLMGateway → FallbackResolver. This task does NOT touch `llm_factory.py` — that's Task 4.

- [ ] **Step 3.1: Write the failing tests**

Create `apps/api/tests/llm_client/test_gateway_fallback.py`:

```python
"""Tests for LLMGateway default_fallback_chain + parse_fallback_chain_env."""
from __future__ import annotations

from unittest.mock import create_autospec

import pytest

from llm_client.gateway import LLMGateway
from llm_client.provider_registry import parse_fallback_chain_env
from llm_client.providers.base import BaseProvider
from llm_client.resolvers import FallbackResolver, PinnedResolver


def _stub(name: str) -> BaseProvider:
    p = create_autospec(BaseProvider, instance=True)
    p.name = name
    return p


# ---- parser ----


def test_parse_none_returns_empty_list() -> None:
    assert parse_fallback_chain_env(None) == []


def test_parse_empty_string_returns_empty_list() -> None:
    assert parse_fallback_chain_env("") == []


def test_parse_single_step() -> None:
    assert parse_fallback_chain_env("minimax:MiniMax-M3") == [
        ("minimax", "MiniMax-M3")
    ]


def test_parse_two_steps() -> None:
    assert parse_fallback_chain_env(
        "minimax:MiniMax-M3,anthropic:claude-haiku-4-5"
    ) == [
        ("minimax", "MiniMax-M3"),
        ("anthropic", "claude-haiku-4-5"),
    ]


def test_parse_strips_whitespace() -> None:
    assert parse_fallback_chain_env(
        " minimax:MiniMax-M3 , anthropic:claude-haiku-4-5 "
    ) == [
        ("minimax", "MiniMax-M3"),
        ("anthropic", "claude-haiku-4-5"),
    ]


def test_parse_rejects_entry_without_colon() -> None:
    with pytest.raises(ValueError, match="expected 'provider:model'"):
        parse_fallback_chain_env("minimax-no-colon")


def test_parse_rejects_empty_provider() -> None:
    with pytest.raises(ValueError, match="empty provider"):
        parse_fallback_chain_env(":MiniMax-M3")


def test_parse_rejects_empty_model() -> None:
    with pytest.raises(ValueError, match="empty model"):
        parse_fallback_chain_env("minimax:")


# ---- LLMGateway fallback chain ----


def test_gateway_no_chain_uses_prefix_resolver() -> None:
    p = _stub("minimax")
    g = LLMGateway(providers={"minimax": p})
    # Default resolver still routes by prefix; not a FallbackResolver.
    from llm_client.resolvers import _PrefixResolver
    assert isinstance(g.default_resolver, _PrefixResolver)
    assert not isinstance(g.default_resolver, FallbackResolver)


def test_gateway_with_chain_builds_fallback_resolver() -> None:
    minimax = _stub("minimax")
    anthropic = _stub("anthropic")
    g = LLMGateway(
        providers={"minimax": minimax, "anthropic": anthropic},
        default_fallback_chain=[
            ("minimax", "MiniMax-M3"),
            ("anthropic", "claude-haiku-4-5"),
        ],
    )
    assert isinstance(g.default_resolver, FallbackResolver)
    # First call returns primary step's provider (placeholder for stream_chat).
    from llm_client.types import ChatRequest, ChatMessage, MessageRole

    req = ChatRequest(
        model="MiniMax-M3",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    assert g.default_resolver(req) is minimax


def test_gateway_skips_steps_with_unregistered_provider() -> None:
    """Steps referencing providers not in the registry are dropped, not fatal.

    Rationale: an operator might enable fallback env in a deployment
    that only has MiniMax configured — we don't want the API to refuse
    to boot. The chain runs with whatever subset is available.
    """
    minimax = _stub("minimax")
    g = LLMGateway(
        providers={"minimax": minimax},
        default_fallback_chain=[
            ("minimax", "MiniMax-M3"),
            ("anthropic", "claude-haiku-4-5"),  # not registered
        ],
    )
    assert isinstance(g.default_resolver, FallbackResolver)
    assert len(g.default_resolver.steps) == 1
    assert g.default_resolver.steps[0].provider is minimax


def test_gateway_raises_when_no_steps_resolve() -> None:
    """All chain entries reference unregistered providers → ValueError at boot."""
    minimax = _stub("minimax")
    with pytest.raises(ValueError, match="at least one step"):
        LLMGateway(
            providers={"minimax": minimax},
            default_fallback_chain=[("anthropic", "claude-haiku-4-5")],
        )
```

- [ ] **Step 3.2: Run tests to verify the new ones fail**

```bash
cd apps/api && python -m pytest tests/llm_client/test_gateway_fallback.py -v
```

Expected: 11 tests fail (no `parse_fallback_chain_env`, no `default_fallback_chain` param).

- [ ] **Step 3.3: Add `parse_fallback_chain_env` to provider_registry**

Append to `apps/api/src/llm_client/provider_registry.py` (before `__all__`):

```python
def parse_fallback_chain_env(value: str | None) -> list[tuple[str, str]]:
    """Parse ``LLM_FALLBACK_CHAIN`` env into ``[(provider_name, model), ...]``.

    Format: ``"provider:model,provider:model[,...]"``. Whitespace is
    stripped around each entry. Empty string and ``None`` return an
    empty list (signals "no fallback configured").

    Raises:
        ValueError: an entry is missing the colon, has an empty
            provider name, or has an empty model name.
    """
    if value is None:
        return []
    result: list[tuple[str, str]] = []
    for raw_entry in value.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError(
                f"fallback chain entry {entry!r}: expected 'provider:model'"
            )
        provider_name, _, model = entry.partition(":")
        provider_name = provider_name.strip()
        model = model.strip()
        if not provider_name:
            raise ValueError(f"fallback chain entry {entry!r}: empty provider")
        if not model:
            raise ValueError(f"fallback chain entry {entry!r}: empty model")
        result.append((provider_name, model))
    return result
```

Update `__all__`:

```python
__all__ = ["build_provider_registry", "parse_fallback_chain_env"]
```

- [ ] **Step 3.4: Add `default_fallback_chain` to LLMGateway**

In `apps/api/src/llm_client/gateway.py`:

1. Extend imports (line 28–29):

```python
from core.logging import get_logger
from llm_client.resolvers import (
    FallbackResolver,
    PinnedResolver,
    Resolver,
    _PrefixResolver,
)
from llm_client.provider_registry import parse_fallback_chain_env  # NEW

log = get_logger(__name__)
```

2. Replace the constructor (lines 49–72) to accept and process the chain:

```python
def __init__(
    self,
    *,
    providers: dict[str, "BaseProvider"],
    default_provider_name: str | None = None,
    default_fallback_chain: list[tuple[str, str]] | None = None,
    attempt_timeout_s: float | None = None,
) -> None:
    if not providers:
        raise RuntimeError("LLMGateway needs at least one provider")
    self._providers: dict[str, "BaseProvider"] = dict(providers)
    if default_provider_name is None:
        # First insertion order (Python 3.7+ dict guarantee). This
        # matches the M1 / M3 factory behavior: MiniMax is preferred
        # over Anthropic when both keys are set.
        default_provider_name = next(iter(self._providers))
    self._default_provider_name = default_provider_name
    self._attempt_timeout_s = attempt_timeout_s

    if default_fallback_chain:
        # Build the chain; skip entries whose provider is not registered
        # (operator might enable the env in a deployment with only one
        # provider configured). Log at WARNING for visibility.
        steps: list[PinnedResolver] = []
        for provider_name, model in default_fallback_chain:
            if provider_name not in self._providers:
                log.warning(
                    "llm_client.fallback_step_skipped",
                    provider=provider_name,
                    model=model,
                    reason="provider not registered",
                )
                continue
            steps.append(
                PinnedResolver(
                    provider=self._providers[provider_name],
                    model=model,
                )
            )
        # Always replace the resolver with FallbackResolver when the
        # chain kwarg is non-empty — even if all steps were skipped
        # downstream, we still want the constructor to fail loudly so
        # operators notice the misconfiguration rather than silently
        # falling back to single-provider mode.
        self._resolver: Resolver = FallbackResolver(
            steps=steps,
            attempt_timeout_s=attempt_timeout_s,
        )
    else:
        # Existing behaviour: prefix-based auto-routing.
        self._resolver = _PrefixResolver(
            providers=self._providers,
            default_provider_name=self._default_provider_name,
        )
```

- [ ] **Step 3.5: Update `__init__.py` exports**

In `apps/api/src/llm_client/__init__.py`, add:

```python
from llm_client.exceptions import AttemptRecord, FallbackChainExhausted
from llm_client.resolvers import FallbackResolver
```

- [ ] **Step 3.6: Run tests to verify they pass**

```bash
cd apps/api && python -m pytest tests/llm_client/test_gateway_fallback.py -v
```

Expected: 11 tests pass.

- [ ] **Step 3.7: Confirm no regressions in existing gateway/resolver/client tests**

```bash
cd apps/api && python -m pytest tests/llm_client/ -v
```

Expected: all tests pass (now ~70 tests in `tests/llm_client/` total).

- [ ] **Step 3.8: Commit**

```bash
cd apps/api && git add src/llm_client/provider_registry.py src/llm_client/gateway.py src/llm_client/__init__.py tests/llm_client/test_gateway_fallback.py
git commit -m "feat(llm-client): LLMGateway default_fallback_chain + env parser (M4.B Task 3)

Wires the env-driven chain end-to-end through the gateway without
yet touching llm_factory (Task 4).

- parse_fallback_chain_env(): strict 'provider:model,provider:model'
  parser with whitespace tolerance and clear ValueError messages
- LLMGateway accepts default_fallback_chain: list[tuple[str,str]] and
  attempt_timeout_s: float|None
- Chain entries referencing unregistered providers are skipped with a
  WARNING log; if ALL entries are skipped the constructor raises
  ValueError so the operator notices the misconfiguration
- 11 unit tests cover: parser (8 cases: None/empty/single/two/whitespace/
  missing-colon/empty-provider/empty-model) + gateway (3 cases: no-chain
  uses PrefixResolver, valid chain builds FallbackResolver, partially-
  registered chain skips the missing step)
- Public exports: FallbackResolver, FallbackChainExhausted, AttemptRecord

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 4: llm_factory wiring + 4 e2e integration tests + new metric

**Files:**
- Modify: `apps/api/src/agent/llm_factory.py:30-52`
- Modify: `apps/api/src/core/config.py:108-107` (add settings fields)
- Modify: `apps/api/src/core/business_metrics.py:60-119` (add LLM_FALLBACK_ATTEMPTS_TOTAL)
- Create: `apps/api/tests/llm_client/integration/test_fallback_e2e.py`

Connects the chain from `Settings` → `LLMGateway` and adds the per-step metric. Includes 4 end-to-end tests via `pytest-httpx` that hit real HTTP paths against mock servers.

- [ ] **Step 4.1: Add settings fields**

In `apps/api/src/core/config.py`, append after the `git_sha` field (line 107):

```python
    # M4.B — LLM fallback chain.
    #
    # ``llm_fallback_chain`` is a comma-separated ``provider:model`` list
    # parsed by :func:`llm_client.provider_registry.parse_fallback_chain_env`.
    # Empty / unset means "no fallback configured" — the gateway uses the
    # existing prefix-based auto-router. When set, the first entry is the
    # primary; subsequent entries are tried on transient failure.
    #
    # ``llm_fallback_attempt_timeout_s`` wraps each step in
    # ``asyncio.wait_for``. Default ``None`` defers to the provider's
    # underlying ``httpx.AsyncClient`` timeout (~60s).
    llm_fallback_chain: str | None = Field(
        default=None, alias="LLM_FALLBACK_CHAIN"
    )
    llm_fallback_attempt_timeout_s: float | None = Field(
        default=None, alias="LLM_FALLBACK_ATTEMPT_TIMEOUT_S"
    )
```

- [ ] **Step 4.2: Add `LLM_FALLBACK_ATTEMPTS_TOTAL` metric**

In `apps/api/src/core/business_metrics.py`, append after `LLM_TOKENS_TOTAL` (line 61) and before the QA metrics section:

```python
# M4.B — fallback chain per-step observability.
#
# Distinct from ``lumen_llm_calls_total`` which records the OUTCOME of
# the whole chain (single provider label = the winner). This counter
# fires once per STEP regardless of chain success, so dashboards can
# see "step 0 failed 5 times today" without needing to correlate with
# the call outcome. Labels:
#   provider: gateway-registered name
#   model:    the step's pinned model (may differ from request.model)
#   step:     0-based index in the chain
#   outcome:  success / provider_unavailable / output_invalid /
#             rate_limited / timeout
# Cardinality ≈ providers × models × chain_length × 5 outcomes.
LLM_FALLBACK_ATTEMPTS_TOTAL = Counter(
    "lumen_llm_fallback_attempts_total",
    "Per-step fallback chain attempts, by provider / model / step / outcome.",
    ("provider", "model", "step", "outcome"),
)
```

Update `__all__`:

```python
__all__ = [
    "LLM_CALLS_TOTAL",
    "LLM_FALLBACK_ATTEMPTS_TOTAL",
    ...
]
```

- [ ] **Step 4.3: Add metric instrumentation to FallbackResolver.ainvoke**

Open `apps/api/src/llm_client/resolvers.py`. Add an import at the top:

```python
from core.business_metrics import LLM_FALLBACK_ATTEMPTS_TOTAL
```

In the `ainvoke` loop, replace the `try` block + `except _FALLBACK_TRIGGERS` block:

```python
        attempts: list[AttemptRecord] = []
        last_exc: BaseException | None = None
        for step_idx, step in enumerate(self.steps):
            rewritten = request.model_copy(update={"model": step.model})
            try:
                if self._timeout is not None:
                    resp = await asyncio.wait_for(
                        step.provider.chat(rewritten),
                        timeout=self._timeout,
                    )
                else:
                    resp = await step.provider.chat(rewritten)
                LLM_FALLBACK_ATTEMPTS_TOTAL.labels(
                    provider=step.provider.name,
                    model=step.model,
                    step=str(step_idx),
                    outcome="success",
                ).inc()
                return resp
            except _FALLBACK_TRIGGERS as exc:
                exc_name = type(exc).__name__
                # Map to metric outcome vocabulary. The Counter labels
                # are bounded strings, not arbitrary class names —
                # anything outside the known set collapses to
                # ``other`` to keep cardinality bounded.
                outcome_label = {
                    "ProviderUnavailable": "provider_unavailable",
                    "OutputInvalid": "output_invalid",
                    "RateLimited": "rate_limited",
                    "TimeoutError": "timeout",
                }.get(exc_name, "other")
                LLM_FALLBACK_ATTEMPTS_TOTAL.labels(
                    provider=step.provider.name,
                    model=step.model,
                    step=str(step_idx),
                    outcome=outcome_label,
                ).inc()
                attempts.append(
                    AttemptRecord(
                        provider_name=step.provider.name,
                        model=step.model,
                        exc_type=exc_name,
                    )
                )
                last_exc = exc
                continue
        # All steps exhausted.
        assert last_exc is not None  # invariant: N>=1 steps always set this
        raise FallbackChainExhausted(attempts=attempts) from last_exc
```

- [ ] **Step 4.4: Write 4 failing e2e tests**

Create `apps/api/tests/llm_client/integration/test_fallback_e2e.py`:

```python
"""End-to-end tests for FallbackResolver chain via pytest-httpx.

These tests spin up a real LLMClient backed by httpx-mocked provider
responses, exercising the full chain path: gateway → FallbackResolver
ainvoke → provider.chat → http → back.
"""
from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from llm_client.client import LLMClient
from llm_client.exceptions import (
    FallbackChainExhausted,
    ProviderUnavailable,
)
from llm_client.gateway import LLMGateway
from llm_client.providers.anthropic_provider import AnthropicProvider
from llm_client.providers.openai_provider import OpenAIProvider
from llm_client.types import ChatMessage, ChatRequest, MessageRole


def _request() -> ChatRequest:
    return ChatRequest(
        model="MiniMax-M3",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )


def _build_gateway_with_chain() -> LLMGateway:
    providers = {
        "minimax": OpenAIProvider(
            api_key="test-key",
            model="MiniMax-M3",
            base_url="https://api.minimaxi.com/v1",
        ),
        "anthropic": AnthropicProvider(
            api_key="test-key",
            model="claude-haiku-4-5",
        ),
    }
    return LLMGateway(
        providers=providers,
        default_fallback_chain=[
            ("minimax", "MiniMax-M3"),
            ("anthropic", "claude-haiku-4-5"),
        ],
    )


def test_e2e_primary_5xx_falls_back(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.minimaxi.com/v1/chat/completions",
        status_code=503,
    )
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": "fallback ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        },
    )

    gateway = _build_gateway_with_chain()
    client = LLMClient(provider_resolver=gateway.default_resolver, tenant_id="t1")
    resp = asyncio_run(client.chat(_request()))

    assert resp.model == "claude-haiku-4-5"
    # Both providers were hit exactly once.
    assert len(httpx_mock.get_requests()) == 2


def test_e2e_primary_timeout_falls_back(httpx_mock: HTTPXMock) -> None:
    # No minimax response — the client times out via wait_for.
    httpx_mock.add_response(
        url="https://api.minimaxi.com/v1/chat/completions",
        status_code=200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": "MiniMax-M3",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "slow"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        },
        # NB: no is_timeout=True — we exercise the per-step timeout via
        # attempt_timeout_s. Add is_timeout to force httpx to hang.
        is_timeout=True,
    )
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": "fallback ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        },
    )

    gateway = _build_gateway_with_chain()
    # Re-construct with an attempt timeout of 0.1s.
    from llm_client.resolvers import FallbackResolver
    gateway._resolver = FallbackResolver(  # type: ignore[attr-defined]
        steps=gateway.default_resolver.steps,
        attempt_timeout_s=0.1,
    )
    client = LLMClient(provider_resolver=gateway.default_resolver, tenant_id="t1")
    resp = asyncio_run(client.chat(_request()))

    assert resp.model == "claude-haiku-4-5"


def test_e2e_all_5xx_raises_chain_exhausted(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.minimaxi.com/v1/chat/completions",
        status_code=503,
    )
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=502,
    )

    gateway = _build_gateway_with_chain()
    client = LLMClient(provider_resolver=gateway.default_resolver, tenant_id="t1")
    with pytest.raises(FallbackChainExhausted) as excinfo:
        asyncio_run(client.chat(_request()))
    assert len(excinfo.value.attempts) == 2


def test_e2e_stream_chat_skips_fallback(httpx_mock: HTTPXMock) -> None:
    """stream_chat() uses the resolver's __call__ (primary only), not ainvoke.

    A 5xx on primary must NOT trigger backup — verify backup is not hit.
    """
    httpx_mock.add_response(
        url="https://api.minimaxi.com/v1/chat/completions",
        status_code=503,
    )

    gateway = _build_gateway_with_chain()
    client = LLMClient(provider_resolver=gateway.default_resolver, tenant_id="t1")

    with pytest.raises(ProviderUnavailable):
        # Drain the async generator.
        async def drain():
            async for _ in client.stream_chat(_request()):
                pass

        asyncio_run(drain())

    # Only minimax was hit — anthropic was NOT consulted.
    assert len(httpx_mock.get_requests()) == 1


def asyncio_run(coro):
    """Helper so test bodies can use top-level await-free style."""
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)
```

- [ ] **Step 4.5: Run e2e tests; verify they fail**

```bash
cd apps/api && python -m pytest tests/llm_client/integration/test_fallback_e2e.py -v -m integration
```

Expected: 4 tests fail (gateway doesn't accept default_fallback_chain yet at this stage? actually it does — Task 3 already wired it. So failures should be specifically about the metric counter and the attempt_timeout wiring).

If they all pass, proceed to Step 4.6. If specific tests fail, investigate before continuing.

- [ ] **Step 4.6: Wire llm_factory**

Modify `apps/api/src/agent/llm_factory.py`:

1. Add import:

```python
from llm_client.provider_registry import (
    build_provider_registry,
    parse_fallback_chain_env,
)
```

2. Replace `_default_llm_client_factory` (lines 30–52):

```python
def _default_llm_client_factory(tenant_id: str) -> LLMClient:
    """Build a per-tenant ``LLMClient`` wrapping the gateway's default resolver.

    When ``settings.llm_fallback_chain`` is set the gateway builds a
    :class:`FallbackResolver` from it; otherwise the existing
    prefix-based auto-router is used. See M4.B spec §5.1.

    Args:
        tenant_id: opaque tenant identifier threaded into ``LLMClient`` so
            usage rows are attributed correctly.

    Raises:
        RuntimeError: if no LLM provider is configured (re-raised from
            ``build_provider_registry``).
    """
    from core.config import get_settings

    settings = get_settings()
    chain = parse_fallback_chain_env(settings.llm_fallback_chain)
    gateway = LLMGateway(
        providers=build_provider_registry(settings),
        default_fallback_chain=chain or None,
        attempt_timeout_s=settings.llm_fallback_attempt_timeout_s,
    )
    return LLMClient(
        provider_resolver=gateway.default_resolver,
        tenant_id=tenant_id,
        usage_recorder=UsageRecorder(),
    )
```

- [ ] **Step 4.7: Run the full llm_client test suite**

```bash
cd apps/api && python -m pytest tests/llm_client/ tests/agent/test_llm_factory.py -v
```

Expected: all tests pass (~80 tests total across `tests/llm_client/` + 5 from `test_llm_factory.py`).

- [ ] **Step 4.8: Commit**

```bash
cd apps/api && git add src/agent/llm_factory.py src/core/config.py src/core/business_metrics.py src/llm_client/resolvers.py tests/llm_client/integration/test_fallback_e2e.py
git commit -m "feat(llm-client): llm_factory wiring + per-step metric (M4.B Task 4)

Connects the chain end-to-end via Settings + llm_factory and adds the
per-step observability metric.

- Settings: llm_fallback_chain (str|None, alias LLM_FALLBACK_CHAIN) and
  llm_fallback_attempt_timeout_s (float|None, alias LLM_FALLBACK_ATTEMPT_TIMEOUT_S)
- business_metrics: LLM_FALLBACK_ATTEMPTS_TOTAL{provider,model,step,outcome}
  fires once per step with bounded outcome vocabulary
  (success/provider_unavailable/output_invalid/rate_limited/timeout/other)
- FallbackResolver.ainvoke instruments the new counter on both success
  and trigger-exception paths
- _default_llm_client_factory reads env, parses the chain, passes to
  LLMGateway alongside attempt_timeout_s
- 4 e2e tests via pytest-httpx:
  * primary 5xx falls back to backup 200
  * primary timeout (via attempt_timeout_s) falls back to backup
  * both providers 5xx raise FallbackChainExhausted with 2 attempts
  * stream_chat uses primary only (anthropic not consulted on 503)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 5: Regression tests + README + memory close-out

**Files:**
- Create: `apps/api/tests/llm_client/test_fallback_regression.py`
- Modify: `README.md:12-35` (status table) + `README.md:303-332` (tech debt list)
- Create: `C:\Users\wma19\.claude\projects\D--work-ai-0401-ai-customer\memory\m4-b-progress.md`
- Modify: `C:\Users\wma19\.claude\projects\D--work-ai-0401-ai-customer\memory\MEMORY.md`

- [ ] **Step 5.1: Write 3 regression tests**

Create `apps/api/tests/llm_client/test_fallback_regression.py`:

```python
"""Regression tests: M4.A behaviour must not change when M4.B is active."""
from __future__ import annotations

import asyncio
from unittest.mock import create_autospec

from llm_client.client import LLMClient, ROUTE_AUTO, ROUTE_PINNED
from llm_client.exceptions import ProviderUnavailable
from llm_client.providers.base import BaseProvider
from llm_client.resolvers import PinnedResolver, _PrefixResolver
from llm_client.types import ChatMessage, ChatRequest, MessageRole


def _stub(name: str) -> BaseProvider:
    p = create_autospec(BaseProvider, instance=True)
    p.name = name
    return p


def test_pinned_resolver_uses_route_mode_pinned() -> None:
    """M4.A PinnedResolver path still labels route_mode='pinned'."""
    p = _stub("minimax")
    p.chat.return_value = _mk_response()
    client = LLMClient(provider_resolver=PinnedResolver(provider=p, model="MiniMax-M3"), tenant_id="t1")
    assert client._route_mode == ROUTE_PINNED
    asyncio.run(client.chat(_mk_request()))
    p.chat.assert_awaited_once()


def test_prefix_resolver_uses_route_mode_auto() -> None:
    """M4.A _PrefixResolver path still labels route_mode='auto'."""
    p = _stub("minimax")
    p.chat.return_value = _mk_response()
    prefix = _PrefixResolver(providers={"minimax": p}, default_provider_name="minimax")
    client = LLMClient(provider_resolver=prefix, tenant_id="t1")
    assert client._route_mode == ROUTE_AUTO
    asyncio.run(client.chat(_mk_request()))
    p.chat.assert_awaited_once()


def test_no_fallback_chain_env_uses_prefix_resolver() -> None:
    """LLMGateway without default_fallback_chain builds a PrefixResolver, not FallbackResolver."""
    from llm_client.gateway import LLMGateway
    from llm_client.resolvers import FallbackResolver

    p = _stub("minimax")
    g = LLMGateway(providers={"minimax": p})
    assert not isinstance(g.default_resolver, FallbackResolver)
    assert isinstance(g.default_resolver, _PrefixResolver)


def _mk_response():
    from llm_client.types import ChatResponse
    return ChatResponse(
        content="ok", model="MiniMax-M3", prompt_tokens=5,
        completion_tokens=3, finish_reason="stop", provider_name="minimax",
    )


def _mk_request() -> ChatRequest:
    return ChatRequest(
        model="MiniMax-M3",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
```

- [ ] **Step 5.2: Run regression tests; verify they pass**

```bash
cd apps/api && python -m pytest tests/llm_client/test_fallback_regression.py -v
```

Expected: 3 tests pass.

- [ ] **Step 5.3: Run the full project test suite — confirm zero regressions**

```bash
cd apps/api && python -m pytest tests/ -q --ignore=tests/llm_client/integration  # skip integration which needs Docker
```

Expected: ~850 tests pass (no new failures; the 2 pre-existing M4.A failures mentioned in `m4-a-progress.md` — `test_embed_empty_list_returns_empty_result` and `test_health_alias_returns_same_body_as_ready` — may still be there; that's OK).

Also run integration tests if Docker is available:

```bash
cd apps/api && python -m pytest tests/llm_client/integration -q -m integration
```

Expected: 4 e2e tests pass.

- [ ] **Step 5.4: Update README status table**

Open `README.md`. After the M4.A row (line 34), add a new M4.B row:

```markdown
| 22 (M4.B) | LLM Gateway fallback chain — `FallbackResolver` wraps list of `PinnedResolver` steps, transparent `ainvoke()` extension to the M4.A resolver seam; `FallbackChainExhausted(ProviderUnavailable)` carries `attempts: list[AttemptRecord]`; per-step metric `lumen_llm_fallback_attempts_total{step,outcome}` | ✅ | 17 (10 unit + 4 integration + 3 regression) |
```

- [ ] **Step 5.5: Add M4.B known tech debt section**

In `README.md`, after the M4.A tech debt list (after line 332), append a new section:

```markdown
### M4.B — Fallback Resolver (deferred to M4.B+)

1. **`stream_chat()` skips fallback** — by design (mid-stream switch is unsafe). When clients need resilient streaming the answer today is "fall back to `chat()` for that turn". Investigate Redis-backed stream continuation only if a customer use case emerges.
2. **No per-step budget** — `attempt_timeout_s` is a single value applied to all steps. Some teams want different timeouts per step (e.g. longer for primary, shorter for backup).
3. **No chain-warm metrics** — only success/failure counts per step; no rolling latency / error-rate. M4.D (budget) likely needs this anyway.
4. **Chain length hardcoded at 2 in tests** — env format supports N (comma-separated), but the resolver is not exercised at N>2 in the test suite. N=2 is the explicit design target per brainstorming Q2; N≥3 needs additional integration tests.
```

- [ ] **Step 5.6: Write `m4-b-progress.md` memory**

Create `C:\Users\wma19\.claude\projects\D--work-ai-0401-ai-customer\memory\m4-b-progress.md`:

```markdown
---
name: m4-b-progress
description: "M4.B — LLM Gateway FallbackResolver (chain on M4.A seam), 5 tasks shipped"
metadata:
  type: project
---

M4.B plan shipped all 5 implementation tasks + close-out. Builds on M4.A's resolver seam without touching LLMClient's public surface.

## Scope

- Task 1: `apps/api/src/llm_client/exceptions.py` + `resolvers.py` — `FallbackResolver` class, `_FALLBACK_TRIGGERS = (ProviderUnavailable, OutputInvalid, RateLimited, asyncio.TimeoutError)`, `FallbackChainExhausted(ProviderUnavailable)` + `AttemptRecord(NamedTuple)`; 10 unit tests
- Task 2: `apps/api/src/llm_client/client.py` — `ROUTE_FALLBACK` constant, `_route_mode_for` extension, `chat()` `hasattr(resolver, "ainvoke")` branch that bypasses internal retry loop, default `max_retries 3 → 1`; 5 unit tests
- Task 3: `provider_registry.parse_fallback_chain_env` + `LLMGateway(default_fallback_chain=, attempt_timeout_s=)`; 11 unit tests (parser + gateway integration)
- Task 4: `Settings.llm_fallback_chain` + `llm_fallback_attempt_timeout_s`, `LLM_FALLBACK_ATTEMPTS_TOTAL{provider,model,step,outcome}` metric, llm_factory env wiring; 4 e2e integration tests via pytest-httpx
- Task 5: 3 regression tests, README status row + 4 known tech debt items, this memory file

## Architecture decisions

- **Resolver protocol unchanged**: `Resolver = Callable[[ChatRequest], BaseProvider]` stays. `FallbackResolver.__call__` returns the primary step's provider as a placeholder so the protocol still type-checks; real chain execution goes through `ainvoke()`. `LLMClient.chat()` detects via `hasattr(resolver, "ainvoke")`. This preserves M4.A's 46 tests with zero changes.
- **`__call__` is NOT async**: even though the chain runs async, the synchronous protocol stays. `ainvoke()` is the async escape hatch.
- **LLMClient chat() skips its retry loop when ainvoke is present**: chain IS the retry. Prevents `max_retries × chain_length = 6+` attempt explosion.
- **FallbackChainExhausted extends ProviderUnavailable**: outer retry loops that already handle ProviderUnavailable treat chain exhaustion as a retryable infrastructure failure.
- **`InvalidRequest` is NOT a fallback trigger**: 4xx is a caller mistake, not transient. Fallback would mask bugs.
- **`max_retries` default 3 → 1**: applies to PinnedResolver/PrefixResolver paths only (the ainvoke branch never reaches the retry loop). Bare-resolver paths are now single-attempt; recommended production config uses the chain.
- **Per-step metric, not per-call**: `LLM_CALLS_TOTAL{route_mode="fallback"}` records the winner; `LLM_FALLBACK_ATTEMPTS_TOTAL{step,outcome}` records every step's success/failure. Lets dashboards see "step 0 failed 5 times today" without correlating with call outcome.
- **Unregistered providers in chain → WARNING + skip**: if chain references a provider not in the registry (e.g. operator enabled fallback env in a MiniMax-only deployment), those steps are dropped with a warning. If ALL steps drop, constructor raises ValueError so the misconfiguration is loud.
- **N steps supported in env, N=2 in tests**: env format `provider:model,provider:model[,...]` accepts arbitrary N; design target is N=2 per brainstorming Q2.

## Workflow

5-task subagent-driven plan. Each task ends with a green test run + commit. Per-task review (spec compliance → code quality) per M4.A precedent.

## Known tech debt remaining (in README §M4.B)

- 4 items: stream_chat() skip / per-step budget / no chain-warm metrics / N=2 only tested

## Test counts

- Total new tests: 33 (10 + 5 + 11 + 4 + 3)
- Pre-existing M4.A 46 tests + all earlier stages: green
- 2 pre-existing M4.A failures (test_embed_empty_list, test_health_alias) untouched

## Why

User picked M4.B (fallback chain) as the next step after M4.A close-out. Cost / reliability value: primary provider outage → automatic failover without per-call code changes; cross-provider fallback (minimax → anthropic) provides regional resilience.

## How to apply

When adding M4.C (BYOK): wrap `FallbackResolver` per-tenant inside `TenantResolver` so each tenant gets its own chain. Reuse the same `_FALLBACK_TRIGGERS` set. When adding M4.D (budget): feed `lumen_llm_fallback_attempts_total` into adaptive chain reordering (demote provider with high step-0 failure rate). Carry forward PII discipline + multi-tenant isolation rules from M1.
```

- [ ] **Step 5.7: Add memory pointer to MEMORY.md**

Open `C:\Users\wma19\.claude\projects\D--work-ai-0401-ai-customer\memory\MEMORY.md`. Add a single line at the end of the bullet list (after the `[M4.A progress]` line):

```markdown
- [M4.B progress](m4-b-progress.md) — M4.B FallbackResolver (chain on M4.A seam), 5 tasks shipped
```

- [ ] **Step 5.8: Final commit**

```bash
cd apps/api && git add tests/llm_client/test_fallback_regression.py README.md
git commit -m "feat(llm-client): M4.B close-out — regression tests + README + memory (Task 5)

- 3 regression tests: PinnedResolver route_mode=pinned unchanged,
  _PrefixResolver route_mode=auto unchanged, no-chain-env gateway
  builds PrefixResolver not FallbackResolver
- README: M4.B status row added to stage table
- README: M4.B tech debt section with 4 deferred items
- Memory: m4-b-progress.md + MEMORY.md pointer
- No production code changes in this task — pure close-out

Co-Authored-By: Claude Code <noreply@anthropic.com>"

# Then commit the memory file separately (lives outside the repo).
```

For the memory file (lives at `C:\Users\wma19\.claude\projects\...`), no git commit needed — it's user-local memory, not part of the repo.

- [ ] **Step 5.9: Final verification — full test suite + git status clean**

```bash
cd apps/api && python -m pytest tests/ -q --ignore=tests/llm_client/integration
cd apps/api && python -m pytest tests/llm_client/integration -q -m integration
git status
```

Expected: all tests pass; git status shows only changes already committed (no uncommitted drift). If there are stragglers, commit them with an explicit fix commit.

---

## Spec Coverage Self-Check

| Spec section | Implemented in |
|--------------|----------------|
| §1 Goals | Tasks 1–4 collectively |
| §2 Non-Goals | Explicitly deferred; documented in §9 tech debt |
| §3 Architecture | Tasks 1 (FallbackResolver) + 2 (ainvoke branch) |
| §4 Component Changes (10 files) | Tasks 1–4 cover all 10 |
| §5.1 Startup (env parsing) | Task 4 Step 4.6 |
| §5.2 Per-call chat | Task 2 Step 2.4 |
| §5.3 Per-call stream_chat (skipped) | Task 4 test_e2e_stream_chat_skips_fallback |
| §5.4 Failure aggregation | Task 1 FallbackChainExhausted |
| §6.1 Trigger exception set | Task 1 _FALLBACK_TRIGGERS |
| §6.2 Per-step timeout | Task 3 LLMGateway kwarg + Task 4 e2e test |
| §6.3 Metric labels (per-call + per-step) | Task 2 ROUTE_FALLBACK + Task 4 LLM_FALLBACK_ATTEMPTS_TOTAL |
| §6.4 Retry interaction | Task 2 Step 2.4 ainvoke branch |
| §6.5 Cross-provider chain entry | Task 3 unit test_gateway_with_chain_builds_fallback_resolver |
| §7.1 Unit tests (10) | Task 1 |
| §7.2 Integration tests (4) | Task 4 |
| §7.3 Regression tests (3) | Task 5 |
| §8 Rollout | Tasks 1–5 (5 commits total) |
| §9 Known Tech Debt | Task 5 README update |
| §10 Cross-References | M4.A reference preserved in spec + memory |

All 19 spec items covered.

## Plan Coverage Self-Check

- [ ] No placeholders ("TBD", "TODO", "implement later", "fill in details") — full code in every step ✓
- [ ] Exact file paths everywhere ✓
- [ ] Exact commands with expected output ✓
- [ ] DRY (no code repetition; tasks build on each other) ✓
- [ ] YAGNI (no speculative features; design target N=2 only) ✓
- [ ] TDD (tests written BEFORE implementation in each task) ✓
- [ ] Frequent commits (one commit per task; 5 commits total) ✓
