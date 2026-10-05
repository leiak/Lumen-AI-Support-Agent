# M4.D — Budget Layer Design

> Per-tenant token budget enforcement on top of M4.C TenantResolver.

**Status:** Spec (brainstorming-approved)
**Date:** 2026-10-05
**Predecessor:** M4.C at commit `7b3cff0`
**Author:** Brainstorming session with user

---

## 1. Goals

M4.C gave each tenant its own LLM provider keys (BYOK). M4.D gives each tenant its own **spending limit**:

1. **Per-tenant monthly hard cap** — each tenant has a configurable `hard_cap_tokens` per calendar month. Calls that would exceed the cap are rejected before invoking the provider.
2. **Soft warning at threshold** — when `tokens_used / hard_cap_tokens >= 0.8`, log a structured warning and increment a metric. Calls continue; ops get early signal.
3. **Admin management** — admins configure the cap and inspect usage via REST endpoints.
4. **Reuse existing seams** — `BudgetResolver` slots into the M4.A `Resolver` pattern that M4.B (fallback chain) and M4.C (tenant resolver) already compose through. No change to `LLMClient.__init__`.

## 2. Non-Goals

- **No real-time rate limiting** (RPM / TPM sliding window). M4.D is monthly aggregate only.
- **No USD-cost computation** — token count is the metering unit. `_COST_PER_1K` in `usage.py` already provides USD estimates for billing reports; M4.D does not surface them.
- **No automatic rollover or top-up** — once the cap is hit, the tenant stays blocked until the next month. Manual admin intervention.
- **No provider-side rate-limit propagation** — provider 429s are surfaced by `RateLimited` exception (M4.A) but not correlated with budget state.
- **No webhooks / PagerDuty** — alerting is metric + structured log only.

## 3. Background

The Lumen AI Customer service has accumulated 4 LLM-related milestones:

- **M4.A** (gateway-core) — `LLMGateway` with prefix-based `Resolver` seam. `LLMClient` consumes a resolver and is unaware of how many providers sit beneath it.
- **M4.B** (fallback-resolver) — `FallbackResolver` composes via the same seam using `ainvoke()`. Chain IS the retry mechanism.
- **M4.C** (tenant-resolver) — `TenantResolver` builds a tenant-private `LLMGateway` from per-tenant `tenant_llm_configs` rows. Fernet-encrypted keys, in-process LRU+TTL cache.

The resolver pattern is the project's load-bearing abstraction for the LLM path. M4.D follows the same composition rule: a new resolver sits **between** the `TenantResolver` and the `LLMClient`, intercepting every call to enforce budget.

### Existing usage data

`LLMUsage` (table `llm_usage`, written by `UsageRecorder`) already records `prompt_tokens + completion_tokens` per call with a `tenant_id` column. This is the source-of-truth token ledger; M4.D reads it via `SUM()` on cache miss and writes to a snapshot table on subsequent reads.

## 4. Architecture

### 4.1 Resolver composition

```
LLMClient.chat(request)
  └─ provider_resolver(request)              ← injected; current M4.A
       │
       └─ BudgetResolver                      ← M4.D new
            ├─ pre-check: budget snapshot vs hard_cap
            │       └─ if used >= hard_cap → raise TenantBudgetExceeded
            ├─ delegate: inner resolver (TenantResolver → FallbackResolver → Provider)
            └─ post-record: increment snapshot.tokens_used by resp.tokens
```

`BudgetResolver` implements the same `Resolver` protocol:
- `__call__(request) -> BaseProvider` for the single-provider path (M4.A backward compat)
- `ainvoke(request) -> ChatResponse` for the chain path (M4.B ainvoke seam)

It wraps whatever resolver sits beneath it (typically `TenantResolver` from M4.C). When `BudgetResolver.ainvoke` is called, it pre-checks, delegates to the inner `ainvoke`, then records the response's token counts into the budget snapshot.

### 4.2 Why a resolver, not a wrapper on `chat()`

- Preserves the M4.A invariant: `LLMClient` consumes a resolver and is unaware of what sits beneath.
- No `LLMClient.__init__` change. Existing call sites (history mining, QA Judge) that inject pinned resolvers stay untouched.
- Naturally handles both single-provider (`__call__` only) and chain (`ainvoke`) tenants.
- Future resolvers (rate limiter, cost optimizer, audit logger) can compose with `BudgetResolver` the same way.

### 4.3 Snapshot table vs live aggregation

`llm_usage` grows by one row per LLM call. A monthly `SUM(prompt_tokens + completion_tokens)` over `llm_usage WHERE tenant_id = ? AND created_at >= month_start` would full-scan without a `(tenant_id, created_at)` index and remains O(rows) per call.

A `tenant_budget_snapshots` table holds `(tenant_id, period, tokens_used)` — incremented in-place as new usage arrives, and refreshed against `llm_usage` only when the cache TTL expires or the snapshot row doesn't exist for the current period.

Trade-off: a small inconsistency window between `tokens_used` and actual usage (≤ TTL = 60s by default, same as M4.C). Acceptable: this is a budget gate, not a billing ledger.

### 4.4 Monthly reset

Each `BudgetResolver._get_snapshot(tenant_id)` call computes the current period as `YYYY-MM` in the tenant's configured timezone (default UTC). If the snapshot row's `period` doesn't match, a new row is inserted and the old one is left untouched (audit trail).

No background job required for reset. The reset is lazy on the next access.

## 5. Components

### 5.1 Data model

**`tenant_budgets`** — one row per tenant (NULL row = unlimited with soft warn only).

| Column | Type | Notes |
|---|---|---|
| `id` | `String(26)` | ULID, PK |
| `tenant_id` | `String(26)` | FK → `tenants.id` ON DELETE CASCADE, UNIQUE |
| `soft_warn_tokens` | `BigInteger` | NULL = no warn. When `tokens_used >= soft_warn_tokens`, fire warning |
| `hard_cap_tokens` | `BigInteger` | NULL = unlimited. When `tokens_used >= hard_cap_tokens`, reject |
| `period_anchor_tz` | `String(64)` | IANA timezone for month boundaries, default `UTC` |
| `updated_at` | `DateTime(tz)` | |

UNIQUE: `tenant_id`.

**`tenant_budget_snapshots`** — one row per (tenant, period).

| Column | Type | Notes |
|---|---|---|
| `id` | `String(26)` | ULID, PK |
| `tenant_id` | `String(26)` | FK → `tenants.id` ON DELETE CASCADE |
| `period` | `String(7)` | `YYYY-MM` |
| `tokens_used` | `BigInteger` | Running total, refreshed on TTL miss |
| `last_refreshed_at` | `DateTime(tz)` | When SUM() last ran against `llm_usage` |

UNIQUE: `(tenant_id, period)`. The repository computes `period_starts_at` on the fly as `period + "-01T00:00:00[period_anchor_tz]"` when materializing a snapshot, so it doesn't need to be stored.

### 5.2 Modules

**`apps/api/src/budget/`** (new package):
- `models.py` — `TenantBudget` + `TenantBudgetSnapshot` ORM models
- `repository.py` — `TenantBudgetRepository` (CRUD) + `TenantBudgetSnapshotRepository` (read/upsert + refresh)
- `resolver.py` — `BudgetResolver` class + `_NoBudgetConfigured` sentinel + `TenantBudgetExceeded` exception
- `cache.py` — `TenantBudgetSnapshotCache` (LRU + TTL, mirrors M4.C `TenantLLMConfigCache`)

**`apps/api/src/llm_client/exceptions.py`** (modified):
- Add `TenantBudgetExceeded(ProviderUnavailable)` carrying `tenant_id` + `period` + `tokens_used` + `hard_cap_tokens` + `period_starts_at`

**`apps/api/src/core/config.py`** (modified):
- Add `tenant_budget_cache_ttl_s` (default 60.0), `tenant_budget_cache_maxsize` (default 1024)

**`apps/api/src/core/business_metrics.py`** (modified):
- Add `LLM_TENANT_BUDGET_EXCEEDED_TOTAL` zero-label counter (rejections at hard cap)
- Add `LLM_TENANT_BUDGET_SOFT_WARN_TOTAL` zero-label counter (soft-warn events)

**`apps/api/src/agent/llm_factory.py`** (modified):
- `_default_llm_client_factory(tenant_id)` wraps the inner `TenantResolver` with `BudgetResolver`
- Resolves the tenant's `TenantBudget` once at factory time (cached, M4.C pattern)
- Failure mode: tenant has no `tenant_budgets` row → no `BudgetResolver` is wrapped (M4.C strict mode still applies; M4.D is opt-in per tenant)

**`apps/api/migrations/versions/17_add_tenant_budgets.py`** (new):
- `down_revision = "16_add_tenant_llm_configs"`
- Creates `tenant_budgets` + `tenant_budget_snapshots`

**`apps/api/src/admin/`** (modified):
- New: `schemas/budget.py` — `TenantBudgetCreate` + `TenantBudgetRead`
- Modified: `api.py` — `POST /admin/tenants/{id}/budget` + `GET /admin/tenants/{id}/budget` + `GET /admin/tenants/{id}/budget/usage`
- Modified: `repository.py` — `AdminTenantBudgetRepository`

### 5.3 The `BudgetResolver`

```python
class BudgetResolver:
    """Wraps an inner resolver with pre-check + post-record budget enforcement."""

    def __init__(
        self,
        *,
        inner: Resolver,
        tenant_id: str,
        budget: TenantBudget | None,  # None = no budget configured
        snapshot_cache: TenantBudgetSnapshotCache,
        snapshot_repo: TenantBudgetSnapshotRepository,
    ) -> None:
        self._inner = inner
        self._tenant_id = tenant_id
        self._budget = budget
        self._snapshot_cache = snapshot_cache
        self._snapshot_repo = snapshot_repo

    def __call__(self, request: ChatRequest) -> BaseProvider:
        self._pre_check()
        return self._inner(request)

    async def ainvoke(self, request: ChatRequest) -> ChatResponse:
        self._pre_check()
        resp = await self._inner.ainvoke(request)
        await self._post_record(resp.prompt_tokens + resp.completion_tokens)
        return resp

    def _pre_check(self) -> None:
        """Raise TenantBudgetExceeded if snapshot.tokens_used >= hard_cap_tokens."""
        if self._budget is None or self._budget.hard_cap_tokens is None:
            return
        snapshot = self._snapshot_cache.get_or_load(self._tenant_id)
        if snapshot.tokens_used >= self._budget.hard_cap_tokens:
            LLM_TENANT_BUDGET_EXCEEDED_TOTAL.inc()
            raise TenantBudgetExceeded(
                tenant_id=self._tenant_id,
                period=snapshot.period,
                tokens_used=snapshot.tokens_used,
                hard_cap_tokens=self._budget.hard_cap_tokens,
                period_starts_at=snapshot.period_starts_at,
            )

    async def _post_record(self, tokens_consumed: int) -> None:
        """Increment snapshot.tokens_used and emit soft-warn if threshold crossed."""
        if tokens_consumed <= 0:
            return
        snapshot = self._snapshot_cache.get_or_load(self._tenant_id)
        new_used = snapshot.tokens_used + tokens_consumed
        await self._snapshot_repo.set_tokens_used(
            tenant_id=self._tenant_id,
            period=snapshot.period,
            tokens_used=new_used,
        )
        # Invalidate cache so next read sees the updated value
        self._snapshot_cache.invalidate(self._tenant_id)
        # Soft warn (idempotent: only fire on threshold CROSS, not every call)
        if (
            self._budget is not None
            and self._budget.soft_warn_tokens is not None
            and snapshot.tokens_used < self._budget.soft_warn_tokens <= new_used
        ):
            LLM_TENANT_BUDGET_SOFT_WARN_TOTAL.inc()
            log.warning(
                "tenant_budget.soft_warn",
                extra={
                    "tenant_id": self._tenant_id,
                    "period": snapshot.period,
                    "tokens_used": new_used,
                    "soft_warn_tokens": self._budget.soft_warn_tokens,
                },
            )
```

**Note on the soft-warn logic:** fires only when `snapshot.tokens_used < soft_warn_tokens <= new_used` — i.e., the threshold was crossed by *this* call. Subsequent calls don't re-fire (avoids log spam).

### 5.4 The `TenantBudgetSnapshotCache`

Mirrors `TenantLLMConfigCache` from M4.C:

- In-process LRU (`maxsize=1024`) + TTL (`60s`).
- `get_or_load(tenant_id)`:
  1. Compute current `period = YYYY-MM`.
  2. If cached snapshot's period matches → return cached.
  3. Else `SELECT * FROM tenant_budget_snapshots WHERE tenant_id = ? AND period = ?`. If row exists → cache + return.
  4. Else `SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) FROM llm_usage WHERE tenant_id = ? AND created_at >= period_start` → INSERT new snapshot row → cache + return.

The SUM() runs only on cache miss (TTL expiry or first access in a new period). Subsequent reads within TTL are pure memory hits.

### 5.5 Admin API

Mirrors M4.C admin endpoints exactly:

| Method | Path | Body / Response |
|---|---|---|
| `POST` | `/api/v1/admin/tenants/{tenant_id}/budget` | `TenantBudgetCreate` → 201 `TenantBudgetRead` |
| `GET` | `/api/v1/admin/tenants/{tenant_id}/budget` | 200 `TenantBudgetRead` (NULL fields = unlimited) |
| `GET` | `/api/v1/admin/tenants/{tenant_id}/budget/usage` | 200 `{period, tokens_used, soft_warn_tokens, hard_cap_tokens}` |

All three require `Depends(require_admin)` with cross-tenant 404 (anti-enumeration), identical to M4.C.

`TenantBudgetRead` exposes only: `soft_warn_tokens`, `hard_cap_tokens`, `period_anchor_tz`, `updated_at`. **No PII / no provider keys** (those are M4.C's concern; M4.D doesn't touch keys).

## 6. Data Flow Walkthrough

**Setup (admin):**
1. Admin POSTs `POST /api/v1/admin/tenants/{id}/budget` with `{soft_warn_tokens: 800000, hard_cap_tokens: 1000000}`.
2. `AdminTenantBudgetRepository.upsert()` writes to `tenant_budgets`.

**Request (LLM call):**
1. Agent calls `LLMClient.chat(request)` via the standard M4.C `TenantResolver` path.
2. Factory wraps the `TenantResolver` with a `BudgetResolver(inner=t_resolver, budget=...)`.
3. `LLMClient.chat()` calls `provider_resolver(request)` → `BudgetResolver.ainvoke(request)`.
4. `BudgetResolver._pre_check()`: snapshot cache hit → snapshot.tokens_used = 500000 → 500000 < 1000000 → proceed.
5. Delegates to `TenantResolver.ainvoke` → `FallbackResolver.ainvoke` → `provider.chat()`.
6. Returns `ChatResponse(prompt_tokens=1000, completion_tokens=500)`.
7. `BudgetResolver._post_record(1500)`: snapshot.tokens_used = 501500. Not at soft_warn threshold yet (800000).
8. Cache invalidated; next read recomputes from snapshot table.

**Hard cap hit:**
1. Tenant at 999500 tokens. New call pre-check: `999500 >= 1000000`? No (just under). Delegates.
2. Response: 600 tokens. `_post_record`: 1000100 >= 1000000. Invalidate cache.
3. Next call: pre-check: `1000100 >= 1000000`? Yes → `TenantBudgetExceeded`. `LLM_TENANT_BUDGET_EXCEEDED_TOTAL.inc()`. Re-raise.

**New month:**
1. March snapshot has `tokens_used = 1000000`.
2. April 1st: next call → cache miss → compute `period = "2026-04"` → snapshot row missing → SUM(llm_usage WHERE created_at >= '2026-04-01') = 0 → INSERT new row → cache.
3. Pre-check: 0 < hard_cap → proceed.

## 7. Error Handling

| Exception | When | Caller handling |
|---|---|---|
| `TenantBudgetExceeded` | `tokens_used >= hard_cap_tokens` at `_pre_check` | API returns 429 (`Too Many Requests`) with structured `{tenant_id, period, tokens_used, hard_cap_tokens}` body. Caller can decide to wait or surface to user. |
| `TenantBudgetNotConfigured` (new) | Tenant has no `tenant_budgets` row | Treated as "no enforcement" — `BudgetResolver` is not even wrapped (factory skips wrapping). |

PII discipline:
- Exception messages include `tenant_id` (opaque ID, not PII), `tokens_used`, `hard_cap_tokens` — all non-sensitive integers/IDs.
- No message content / prompt text / API keys appear in budget path.
- `LLM_TENANT_BUDGET_*` metrics are zero-label (no `tenant_id` in labels).

## 8. Cardinality Discipline

| Metric | Labels | Cardinality bound |
|---|---|---|
| `lumen_llm_tenant_budget_exceeded_total` | (none) | 1 series |
| `lumen_llm_tenant_budget_soft_warn_total` | (none) | 1 series |

`tenant_id` stays in log lines (`extra={...}`), not metric labels. Mirrors M4.C's `LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL` discipline.

## 9. Testing

### 9.1 Unit tests

| Test | File |
|---|---|
| `TenantBudgetSnapshotRepository.refresh` runs `SUM()` correctly | `tests/budget/test_repository.py` |
| `TenantBudgetSnapshotCache` hit/miss/TTL/LRU | `tests/budget/test_cache.py` |
| `BudgetResolver._pre_check` raises `TenantBudgetExceeded` at cap | `tests/budget/test_resolver.py` |
| `BudgetResolver._post_record` increments + soft-warns on threshold cross | `tests/budget/test_resolver.py` |
| `BudgetResolver._post_record` does NOT re-fire soft warn on subsequent calls | `tests/budget/test_resolver.py` |
| `TenantBudgetExceeded` exception shape (tenant_id, period, tokens_used, hard_cap_tokens) | `tests/budget/test_resolver.py` |

### 9.2 Integration tests

| Test | File |
|---|---|
| New tenant has no `tenant_budgets` row → factory skips `BudgetResolver` wrap | `tests/agent/test_llm_factory_budget.py` |
| Tenant with `hard_cap_tokens=100` → 10-call budget exhausts on call 11 | `tests/agent/test_llm_factory_budget.py` |
| `LLM_TENANT_BUDGET_EXCEEDED_TOTAL` increments on cap hit | `tests/agent/test_llm_factory_budget.py` |
| `LLM_TENANT_BUDGET_SOFT_WARN_TOTAL` increments once on threshold cross | `tests/agent/test_llm_factory_budget.py` |

### 9.3 E2E tests (pytest-httpx)

| Test | File |
|---|---|
| Tenant at hard cap → `LLMClient.chat()` raises `TenantBudgetExceeded`, no provider HTTP traffic observed | `tests/budget/integration/test_budget_e2e.py` |
| Tenant's prior-month usage does not affect current-month cap | `tests/budget/integration/test_budget_e2e.py` |
| Soft warn fires exactly once per period (not per call above threshold) | `tests/budget/integration/test_budget_e2e.py` |

### 9.4 Admin API tests

| Test | File |
|---|---|
| POST creates `tenant_budgets` row | `tests/admin/test_budget_api.py` |
| POST upserts (same tenant → updates `updated_at`) | `tests/admin/test_budget_api.py` |
| GET returns current budget (no PII fields) | `tests/admin/test_budget_api.py` |
| GET `/usage` returns `{period, tokens_used, ...}` | `tests/admin/test_budget_api.py` |
| POST without admin token → 401 | `tests/admin/test_budget_api.py` |
| POST with cross-tenant admin → 404 (anti-enumeration) | `tests/admin/test_budget_api.py` |

### 9.5 Migration tests

| Test | File |
|---|---|
| `tenant_budgets` and `tenant_budget_snapshots` exist post-upgrade | `tests/db/test_alembic_env.py` (extend existing) |
| CASCADE delete on tenant removes both tables' rows | `tests/budget/test_repository.py` |

## 10. Rollout

1. Migration `17_add_tenant_budgets.py` — additive, no behavior change.
2. Admin API + repositories — opt-in (no budget row = no enforcement).
3. Factory wiring — wraps `BudgetResolver` only when `TenantBudget` exists. Default (no row) = behavior unchanged.
4. Existing tenants see no behavior change. New tenants get explicit "no budget" until admin configures.
5. Soft-warn metric + log start emitting as soon as any tenant has a `soft_warn_tokens` configured.

Rollback: drop the two tables + revert factory wrap. No data loss (additive).

## 11. Known Tech Debt (deferred)

1. **No automatic period reset job** — reset is lazy on next access. If a tenant goes silent for 2 months, the stale period row stays. Acceptable: rows accumulate ≤ 12/year/tenant.
2. **No top-up mechanism** — once hit, the tenant stays blocked until manual admin action (set `hard_cap_tokens` higher) or month rollover.
3. **Snapshot inconsistency window** — ≤ TTL (60s default). Within that window, a tenant could go slightly over the cap before being rejected.
4. **Soft-warn is per-period, not sticky** — fires once per period on threshold cross; if admin lowers the cap mid-period, the warn may re-fire or not fire correctly.
5. **No per-model breakdown** — budget is total tokens; can't enforce "max 100k Sonnet, 1M Haiku".
6. **No budget for non-LLM costs** (KB retrieval, embedding) — out of scope; LLM only.
7. **Snapshot refresh doesn't lock** — concurrent SUM() calls could double-insert. Mitigated by UNIQUE constraint + ON CONFLICT.
8. **No integration with provider 429s** — provider rate limits are surfaced but don't update the budget state.

## 12. File Map (for plan)

```
apps/api/src/budget/
├── __init__.py                    # NEW
├── models.py                      # NEW (ORM)
├── repository.py                  # NEW (TenantBudgetRepository + SnapshotRepository)
├── cache.py                       # NEW (LRU+TTL snapshot cache)
└── resolver.py                    # NEW (BudgetResolver + exceptions)

apps/api/src/llm_client/exceptions.py        # MODIFIED (add TenantBudgetExceeded)
apps/api/src/core/config.py                  # MODIFIED (cache TTL + maxsize)
apps/api/src/core/business_metrics.py        # MODIFIED (2 new zero-label counters)
apps/api/src/agent/llm_factory.py            # MODIFIED (wrap with BudgetResolver)
apps/api/src/admin/api.py                    # MODIFIED (3 new endpoints)
apps/api/src/admin/repository.py             # MODIFIED (AdminTenantBudgetRepository)
apps/api/src/admin/schemas/budget.py         # NEW

apps/api/migrations/versions/17_add_tenant_budgets.py  # NEW

apps/api/tests/budget/test_repository.py     # NEW
apps/api/tests/budget/test_cache.py          # NEW
apps/api/tests/budget/test_resolver.py       # NEW
apps/api/tests/budget/integration/test_budget_e2e.py  # NEW
apps/api/tests/agent/test_llm_factory_budget.py       # NEW
apps/api/tests/admin/test_budget_api.py      # NEW

README.md                                     # MODIFIED (M4.D row + tech debt)
~/.claude/.../memory/m4-d-progress.md         # NEW
~/.claude/.../memory/MEMORY.md                # MODIFIED (pointer)
```

---

**Spec complete.** Ready for plan.
