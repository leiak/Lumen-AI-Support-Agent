# M4.B — LLM Gateway Fallback Resolver Design

> Spec for adding a chain-based fallback resolver to the LLM Gateway, layered on top of the M4.A resolver seam. See [[2026-09-20-m4-a-gateway-core-design]] for the seam this spec extends.

**Status:** Draft
**Author:** brainstorming session 2026-10-04
**Predecessor:** M4.A shipped at commit f1a63ae

---

## Confirmed assumptions

These were confirmed during brainstorming and are baked into the design below. Flag any objection during spec review.

| # | Assumption | Source |
|---|------------|--------|
| A1 | Fallback triggers on **all 4** transient exception types: `ProviderUnavailable`, `OutputInvalid`, `RateLimited`, `asyncio.TimeoutError` | Q1 answer: all four |
| A2 | Chain is **global, N steps** (design target N=2), configured via env (`LLM_FALLBACK_CHAIN=provider:model,provider:model[,...]`) | Q2 answer |
| A3 | Chain entries may reference **different providers** (cross-provider fallback) | Q3 answer |
| A4 | Fallback applies to **`chat()` only**. `stream_chat()` uses primary only. | Q4 answer |
| A5 | When all steps fail, raise **`FallbackChainExhausted`** — a new exception that **extends `ProviderUnavailable`** and carries `attempts: list[AttemptRecord]` | Q5 answer |
| A6 | `FallbackChainExhausted extends ProviderUnavailable` so `LLMClient.chat()` retry path treats chain exhaustion as a retryable failure → **retries the whole chain** (叠加) | Q6 answer |
| A7 | `LLMClient.chat()` default `max_retries` reduced **from 3 to 1** to avoid `max_retries × chain_length = 6` attempts being too aggressive | Designer recommendation, accepted |
| A8 | New metric label `step` added per attempt for per-step observability (Prometheus cardinality doubles; acceptable per spec) | Designer recommendation, accepted |
| A9 | `InvalidRequest` (4xx-equivalent) does **NOT** trigger fallback — it's a caller error, not transient | Designer recommendation, accepted |

---

## 1. Goals

- Allow a primary LLM provider to fail over to a secondary on transient errors (5xx, 429, timeout, malformed response).
- Keep the `Resolver = Callable[[ChatRequest], BaseProvider]` protocol signature **unchanged** so M4.A's 46 tests and all call sites stay green.
- Make fallback **opt-in** via global env config (`LLM_FALLBACK_CHAIN`); absent env ⇒ current behavior. Env format `provider:model,provider:model[,...]` supports arbitrary `N`, but the **design target is N=2** and the test suite only covers N=2 (see §9 #4).
- Aggregate failures into a single new exception (`FallbackChainExhausted`) that preserves the attempt history for ops triage.
- Layer cleanly onto the resolver seam — no `LLMClient.__init__` signature changes, no `_resolve_request` rewrites for non-fallback resolvers.

## 2. Non-Goals (explicitly deferred)

- **Per-tenant fallback configuration** — M4.C BYOK (DB-backed `TenantResolver` wrapping `FallbackResolver`).
- **Mid-stream fallback for `stream_chat()`** — once any byte has been streamed, resuming on another provider is not safe. Skipped entirely in this spec.
- **Cost / token-budget enforcement** — M4.D.
- **Dynamic chain reordering** (e.g., adaptive based on latency history) — M4.D+ if needed.

## 3. Architecture

```
┌────────────────────────────────────────────────────────────────┐
│ LLMClient.chat(request)                                        │
│   ├─ _resolve_request(request)                                │
│   │     ├─ isinstance(PinnedResolver)    → route_mode="pinned"│
│   │     ├─ isinstance(FallbackResolver)  → route_mode="fallback" ← NEW
│   │     └─ else                           → route_mode="auto"   │
│   │                                                            │
│   ├─ if hasattr(provider_resolver, "ainvoke"):                 │
│   │     return await resolver.ainvoke(request) ← NEW whole    │
│   │     (LLMClient does NOT wrap in its own retry loop)        │
│   │                                                            │
│   └─ else: existing chat() retry loop (unchanged)              │
└────────────────────────────────────────────────────────────────┘
              │
              ▼
┌────────────────────────────────────────────────────────────────┐
│ FallbackResolver.ainvoke(request)                             │
│   attempts: list[AttemptRecord] = []                          │
│   for step_idx, step in enumerate(self.steps):                │
│     try:                                                       │
│       rewritten = request.model_copy(update={"model": step.model}) │
│       if self._timeout is not None:                            │
│         resp = await asyncio.wait_for(                         │
│             step.provider.chat(rewritten), self._timeout)      │
│       else:                                                    │
│         resp = await step.provider.chat(rewritten)             │
│       _record_step_success(step_idx, step, resp)               │
│       return resp                                              │
│     except _FALLBACK_TRIGGERS as e:                            │
│       _record_step_failure(step_idx, step, type(e).__name__)   │
│       attempts.append(AttemptRecord(                           │
│         provider_name=step.provider.name,                      │
│         model=step.model,                                      │
│         exc_type=type(e).__name__))                            │
│   raise FallbackChainExhausted(attempts=attempts)              │
└────────────────────────────────────────────────────────────────┘
```

**Why opt-in via `ainvoke` and not via the protocol change**:
The M4.A contract `Resolver = Callable[[ChatRequest], BaseProvider]` has 46 tests + 4 call sites that all depend on it. Adding a chain orchestrator inside that protocol would force every resolver to be `async` and would break `isinstance(resolver, PinnedResolver)` detection in `LLMClient._resolve_request`. Keeping `__call__` synchronous (returns first step's provider as a placeholder) and exposing `ainvoke` as a duck-typed extension preserves backwards compatibility 100%.

## 4. Component Changes

| File | Change | Notes |
|------|--------|-------|
| `apps/api/src/llm_client/exceptions.py` | Add `FallbackChainExhausted(ProviderUnavailable)` + `AttemptRecord(NamedTuple)` | Two classes; ~30 lines |
| `apps/api/src/llm_client/resolvers.py` | Add `FallbackResolver` class + `_FALLBACK_TRIGGERS` tuple + `ROUTE_FALLBACK` constant | ~120 lines |
| `apps/api/src/llm_client/gateway.py` | (a) Add `default_fallback_chain` constructor kwarg; (b) build `FallbackResolver` when set, else existing `_PrefixResolver`; (c) export `__all__` update | ~30 lines delta |
| `apps/api/src/llm_client/provider_registry.py` | Add `parse_fallback_chain_env(value: str \| None) -> list[tuple[str, str]]` | ~25 lines |
| `apps/api/src/llm_client/client.py` | (a) `ROUTE_FALLBACK` constant; (b) `_route_mode_for` extension; (c) `chat()` `hasattr(resolver, "ainvoke")` branch; (d) default `max_retries=1`; (e) `stream_chat()` unchanged | ~25 lines delta |
| `apps/api/src/llm_client/__init__.py` | Export `FallbackResolver`, `FallbackChainExhausted`, `AttemptRecord` | 3 lines |
| `apps/api/src/agent/llm_factory.py` | Read `LLM_FALLBACK_CHAIN` env, pass to `LLMGateway(..., default_fallback_chain=...)` | ~10 lines |
| `apps/api/src/core/config.py` | Add `llm_fallback_chain: str \| None = None` + `llm_fallback_attempt_timeout_s: float \| None = None` | 2 fields |
| `apps/api/src/core/business_metrics.py` | Add `LLM_FALLBACK_ATTEMPTS_TOTAL` Counter with labels `(provider, model, step, outcome)` | ~10 lines |
| `README.md` | Add M4.B status row + update known tech debt list | docs only |

No DB migration. No frontend change. No new top-level module.

## 5. Data Flow

### 5.1 Startup (one-time)

```python
# apps/api/src/agent/llm_factory.py — _default_llm_client_factory
settings = get_settings()
providers = build_provider_registry(settings)
gateway = LLMGateway(
    providers=providers,
    default_provider_name=settings.default_provider,
    default_fallback_chain=(
        parse_fallback_chain_env(settings.llm_fallback_chain)
        if settings.llm_fallback_chain
        else None
    ),
)
# When default_fallback_chain is set:
#   self._resolver = FallbackResolver(
#       steps=[PinnedResolver(provider=registry[n], model=m)
#              for (n, m) in default_fallback_chain
#              if n in registry],
#       attempt_timeout_s=settings.llm_fallback_attempt_timeout_s,
#   )
# Else: self._resolver = _PrefixResolver(...)  — unchanged.
```

`parse_fallback_chain_env("minimax:MiniMax-M3,anthropic:claude-haiku-4-5")` →
`[("minimax", "MiniMax-M3"), ("anthropic", "claude-haiku-4-5")]`.

Validation: rejects empty string, rejects malformed entries (missing colon), warns (does not raise) when a referenced provider is not in the registry — those steps are skipped, log at WARNING with `llm_client.fallback_step_skipped`.

### 5.2 Per-call (chat)

```
LLMClient.chat(req)
  │
  ├─ _resolve_request(req)
  │   └─ route_mode = "fallback"   (isinstance FallbackResolver)
  │
  ├─ hasattr(provider_resolver, "ainvoke") → True
  │   └─ try:
  │        resp = await provider_resolver.ainvoke(req)
  │        usage.enqueue(provider=resp_provider, model=resp.model, tokens=...)
  │        _record_success(provider, model, route_mode, p_tok, c_tok)
  │        return resp
  │      except FallbackChainExhausted as e:
  │        _record_failure("<unknown>", req.model, route_mode, "unavailable")
  │        raise   # LLMClient does NOT retry (chain IS the retry)
  │
  └─ else: existing chat() retry loop  ← unchanged
```

**Important**: when `provider_resolver` is a `FallbackResolver`, `LLMClient.chat()` does **not** enter its own retry loop. The chain's internal iteration replaces retry. This avoids `max_retries × chain_length` attempt explosion.

### 5.3 Per-call (stream_chat)

Unchanged. `LLMClient.stream_chat()` always uses `provider_resolver(request)` which returns the **first step's** provider (the primary). No fallback. A 5xx on primary → exception propagates to caller immediately. This is documented in the `stream_chat` docstring.

### 5.4 Failure aggregation

When all steps fail, `FallbackChainExhausted(attempts=[...])` carries:

```python
class AttemptRecord(NamedTuple):
    provider_name: str   # e.g. "minimax"
    model: str           # e.g. "MiniMax-M3"
    exc_type: str        # e.g. "ProviderUnavailable" / "RateLimited" / "TimeoutError"
```

Callers (e.g. `agent/runtime.py`) catch `FallbackChainExhausted` and surface the standard `FALLBACK_MESSAGE` to the user, same as today's 5xx fallback. The `attempts` list is logged at WARNING with `llm_client.fallback_chain_exhausted` for ops triage (PII discipline: provider names + error class names only — no message content, no request bodies).

## 6. Behavior Details

### 6.1 Trigger exception set

```python
_FALLBACK_TRIGGERS = (
    ProviderUnavailable,   # 5xx, transport errors
    OutputInvalid,         # unparseable response
    RateLimited,           # 429
    asyncio.TimeoutError,  # wait_for timeout
)
```

`InvalidRequest` (4xx-equivalent) is **not** a trigger. Rationale: 4xx is a caller error (bad schema, wrong model name, content policy violation). Fallback would mask the bug and waste tokens on the secondary. Surface immediately.

### 6.2 Per-step timeout

`attempt_timeout_s` (default `None`):
- When set, each step's `provider.chat()` is wrapped in `asyncio.wait_for(...)`.
- `TimeoutError` is then caught by the same handler as `ProviderUnavailable`.
- Default `None` defers to the underlying `httpx.AsyncClient` default (~60s) — same as today's behavior.

### 6.3 Metric labels

Existing labels per M4.A are reused; one new label and one new metric.

**Per-call outcome** (via `_record_success` / `_record_failure` in `LLMClient`):

| Call result | `provider` | `model` | `route_mode` | `outcome` |
|-------------|-----------|---------|--------------|-----------|
| Primary succeeds | `minimax` | `MiniMax-M3` | `fallback` | `success` |
| Primary fails, backup succeeds | `anthropic` | `claude-haiku-4-5` | `fallback` | `success` |
| All steps fail | `<unknown>` | req.model | `fallback` | `unavailable` |

**Per-step outcome** (new metric, `LLM_FALLBACK_ATTEMPTS_TOTAL`):

| Step result | `provider` | `model` | `step` | `outcome` |
|-------------|-----------|---------|--------|-----------|
| Step 0 success | `minimax` | `MiniMax-M3` | `0` | `success` |
| Step 0 fail (5xx) | `minimax` | `MiniMax-M3` | `0` | `provider_unavailable` |
| Step 1 success | `anthropic` | `claude-haiku-4-5` | `1` | `success` |
| Step 1 fail | `anthropic` | `claude-haiku-4-5` | `1` | `rate_limited` |

`outcome` values for the per-step metric are: `success`, `provider_unavailable`, `output_invalid`, `rate_limited`, `timeout`. PII discipline: no message content.

### 6.4 Retry interaction

With `FallbackChainExhausted extends ProviderUnavailable`:
- An external caller that wraps `LLMClient.chat()` in its own retry (none currently exists) would retry the whole chain. The `LLMClient.chat()` retry loop **itself does not run** when fallback is engaged (we exit before the loop), so there's no `3 × 2 = 6` attempt storm.

The `LLMClient.chat()` default `max_retries=3 → 1` change applies **only to the non-fallback path** (the existing `_PrefixResolver` and `PinnedResolver` code paths). Their behavior with `max_retries=1` is one attempt — same effective reliability as today's chain-fallback path. We accept this slight behavior change because fallback is the recommended configuration going forward; the bare `_PrefixResolver` path is now "single-attempt, no chain".

### 6.5 Cross-provider chain entry

Each step is a `(provider_name, model)` tuple. `PinnedResolver` instances inside the chain carry their own model name, so the metric label reflects the actual model used per step. Cross-provider (e.g., `minimax → anthropic`) is fully supported; the wire format differs per provider but that's each `BaseProvider` subclass's concern, not the resolver's.

## 7. Testing Strategy

### 7.1 Unit tests — `tests/llm_client/test_fallback_resolver.py` (10 tests)

1. `test_constructor_rejects_empty_steps` — `FallbackResolver(steps=[])` raises `ValueError`.
2. `test_constructor_rejects_duplicate_steps` — two steps with same `(provider.name, model)` raises `ValueError`.
3. `test_first_step_success_returns_immediately` — stub provider 0 returns ChatResponse, stub provider 1 NOT called (`mock.call_count == 0`).
4. `test_provider_unavailable_triggers_fallback` — provider 0 raises `ProviderUnavailable`, provider 1 returns ChatResponse.
5. `test_output_invalid_triggers_fallback` — provider 0 raises `OutputInvalid`, provider 1 returns.
6. `test_rate_limited_triggers_fallback` — provider 0 raises `RateLimited`, provider 1 returns.
7. `test_timeout_triggers_fallback` — provider 0's chat sleeps 5s with `attempt_timeout_s=0.1`, provider 1 returns.
8. `test_invalid_request_does_NOT_trigger_fallback` — provider 0 raises `InvalidRequest`, `InvalidRequest` propagates (no call to provider 1).
9. `test_all_steps_fail_raises_chain_exhausted` — both providers raise; `FallbackChainExhausted.attempts` has 2 `AttemptRecord`s with correct `(provider, model, exc_type)`.
10. `test_chain_exhausted_is_provider_unavailable` — `issubclass(FallbackChainExhausted, ProviderUnavailable) is True`.

### 7.2 Integration tests — `tests/llm_client/integration/test_fallback_e2e.py` (4 tests via `pytest-httpx`)

11. `test_e2e_primary_5xx_falls_back` — minimax mock returns 503, anthropic mock returns 200; verify response came from anthropic + metric `route_mode="fallback"` + `step=1` counter incremented.
12. `test_e2e_primary_timeout_falls_back` — minimax mock sleeps past `attempt_timeout_s`, anthropic returns 200.
13. `test_e2e_all_5xx_raises_chain_exhausted` — both mocks return 503; `FallbackChainExhausted` with 2 attempts raised; `lumen_llm_fallback_attempts_total{outcome="provider_unavailable"}` ×2.
14. `test_e2e_stream_chat_skips_fallback` — primary 503 on stream; verify anthropic mock NOT called; `ProviderUnavailable` raised from `stream_chat`.

### 7.3 Regression tests (3 tests, in existing files)

15. `test_pinned_resolver_still_works` — `LLMClient` with a `PinnedResolver` resolver still uses `route_mode="pinned"` and rewrites `request.model`.
16. `test_prefix_resolver_still_works` — `LLMClient` with `_PrefixResolver` still uses `route_mode="auto"` and retry loop.
17. `test_no_fallback_chain_env_uses_prefix_resolver` — `LLMGateway(default_fallback_chain=None)` sets `default_resolver` to `_PrefixResolver`, not `FallbackResolver`.

### 7.4 Existing tests

All M4.A 46 tests + M3 admin/qa/vision/PDF tests + M2.B tests + M1 tests must remain green. Run via `pytest tests/ -q` after each task.

## 8. Rollout

1. **Spec committed** to `docs/superpowers/specs/2026-10-04-m4-b-fallback-resolver-design.md`.
2. **Plan written** via `superpowers:writing-plans` skill, split into 4–6 subagent tasks (matches M4.A's task structure).
3. **Implementation** — subagent-driven, two-stage review per task (spec compliance → code quality), same workflow as M4.A.
4. **Tests** — unit + integration + regression green before merge.
5. **README update** — add M4.B status row + 1 known tech debt item (stream fallback deferred).
6. **Memory** — add `m4-b-progress.md` + MEMORY.md pointer.

No DB migration. No frontend change. No infrastructure change. Feature flag is `LLM_FALLBACK_CHAIN` env — empty = current behavior.

## 9. Known Tech Debt (post-M4.B)

These will go into README's `### M4.B —` section:

1. **`stream_chat()` skips fallback** — design choice (mid-stream switch is unsafe). When clients need resilient streaming, the answer today is "fall back to chat() for that turn". M4.B+ may investigate Redis-backed stream continuation if a customer use case emerges.
2. **No per-step budget** — `attempt_timeout_s` is a single value applied to all steps. Some teams want different timeouts per step (e.g., longer for primary, shorter for backup). Deferred.
3. **No chain-warm metrics** — only success/failure counts per step; no rolling latency / error-rate. M4.D (budget) likely needs this anyway.
4. **Chain length hardcoded at 2** — env format supports N (comma-separated), but the resolver is not exercised at N>2 in tests. N=2 is the explicit design target per brainstorming Q2; N≥3 needs additional integration tests.

## 10. Cross-References

- M4.A design: [[2026-09-20-m4-a-gateway-core-design]] — defines the resolver seam this spec extends.
- M4.A tech debt #24 (per-call `httpx.AsyncClient`) — orthogonal; deferred to M4.C. Fallback chain does NOT solve HTTP pool reuse.
- M4.C (BYOK) — `TenantResolver` will likely wrap `FallbackResolver` to support per-tenant chains.
- M4.D (budget) — `LLM_FALLBACK_ATTEMPTS_TOTAL{step, outcome}` is the input signal for adaptive chain reordering (e.g., demote a provider with high failure rate).
