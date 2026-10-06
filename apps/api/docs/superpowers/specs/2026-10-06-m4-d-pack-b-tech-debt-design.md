# M4.D Pack B Tech Debt Follow-up Design

> **Spec status:** Draft (awaiting user review)
> **Pack:** M4.D Pack B
> **Closes tech debt items:** #2 (no top-up mechanism), #5 (no per-model breakdown), #8 (no provider 429 integration)
> **Defers:** #6 (no non-LLM cost budget) → Pack C
> **Depends on:** M4.D Pack A (committed) — Pack A's `BudgetResolver._pre_check` / `_post_record` / sticky soft-warn / `TenantBudgetSnapshotRepository.refresh()` with `pg_advisory_xact_lock` are the substrate this pack builds on.
> **Single PR, single release.**

---

## 1. Goals

Pack B closes 3 of the 4 deferred M4.D tech debt items:

| # | Item | Pack B Treatment |
|---|------|------------------|
| 2 | No top-up mechanism | Manual credit grant by super_admin, current-month only, immutable audit log. Effective cap = base + credits. |
| 5 | No per-model breakdown | Read-only observability: lazy `GROUP BY` on `llm_usage` with 30s in-process cache, surfaced via `?breakdown=true` on existing snapshot endpoint. No cap enforcement. |
| 8 | No provider 429 integration | Post-mortem budget gate: when `FallbackResolver` raises `RateLimited` and remaining budget is below a configurable threshold, raise `TenantBudgetRateLimited` (HTTP 429 + counter). Otherwise let 429 propagate. |

Out of scope: any change to M4.B's `FallbackResolver` chain semantics, M4.C's `TenantResolver` / Fernet, or Pack A's pre-check / soft-warn / cleanup logic. Pack B is purely additive.

---

## 2. Architectural overview

```
                ┌──────────────────────────────────────┐
                │       admin SPA / super_admin        │   (consumers)
                └───────────────┬──────────────────────┘
                                │ REST
                ┌───────────────▼──────────────────────┐
                │      admin/api.py (Pack A + Pack B)  │
                │  POST /admin/tenants/{tid}/credits   │  ← #2 (new, super_admin only)
                │  GET  /admin/tenants/{tid}/credits   │  ← #2 (new, super_admin only)
                │  GET  /admin/tenants/{tid}/budget/   │  ← #5 (extend w/ ?breakdown=true)
                │       usage                          │
                └───────────────┬──────────────────────┘
                                │
        ┌───────────────────────┼─────────────────────┐
        │                       │                     │
        ▼                       ▼                     ▼
┌──────────────┐        ┌──────────────┐     ┌──────────────────────┐
│ CreditService│        │PerModelSvc + │     │  BudgetResolver      │
│ + sum_for_   │        │30s cache     │     │  (Pack A + Pack B)   │
│   period     │        │ (no table)   │     │  + _compute_eff_cap  │
│ (immutable   │        └──────┬───────┘     │  + _maybe_raise_gate │
│  audit)      │               │             │  + TenantBudgetRate  │
│              │               │             │    Limited exception │
└──────┬───────┘               │             └────────┬─────────────┘
       │                       │                      │
       ▼                       ▼                      ▼
┌────────────────────────────────────────────────────────────────────┐
│ PostgreSQL                                                       │
│  + tenant_budget_credits (#2, immutable append-only)              │
│  tenant_budgets (Pack A) + tenant_budget_snapshots (Pack A)       │
│  llm_usage (existing — source for #5 lazy query)                  │
└────────────────────────────────────────────────────────────────────┘
```

**Three independent subsystems, single PR.** None of #2/#5/#8 depend on each other for correctness; #5 invalidates on #2 grant as a UX nicety. Implementation order: **#2 → #5 → #8** (so #8's gate observes the full effective-cap semantics including credits).

---

## 3. Task 1 — #2 Top-up Credit Grant

### 3.1 Schema

New migration `apps/api/migrations/versions/19_add_tenant_budget_credits.py`:

```sql
CREATE TABLE tenant_budget_credits (
    id          VARCHAR(26) PRIMARY KEY,            -- ULID
    tenant_id   VARCHAR NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    period      VARCHAR(7) NOT NULL,                -- YYYY-MM (denormalized for index locality)
    tokens      BIGINT NOT NULL CHECK (tokens > 0),
    note        TEXT NOT NULL,
    granted_by  VARCHAR NOT NULL,                   -- user_id of super_admin
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_tenant_budget_credits_tenant_period
    ON tenant_budget_credits(tenant_id, period);
```

`down_revision = "18_add_soft_warn_fired_at"` (Pack A's last migration).

Append-only: no UPDATE/DELETE exposed in application code. Future revocations (if needed) are new rows with negative `tokens` (out of scope for Pack B but the schema accommodates).

### 3.2 ORM (`apps/api/src/budget/models.py`)

Add `TenantBudgetCredit` class alongside existing models:

```python
class TenantBudgetCredit(Base):
    __tablename__ = "tenant_budget_credits"
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    granted_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
```

Index `ix_tenant_budget_credits_tenant_period` declared via `__table_args__`.

### 3.3 CreditService (`apps/api/src/budget/credits.py`)

```python
from budget.resolver import _current_period   # shared with resolver for period-bound semantics

class CreditService:
    def __init__(self, session: AsyncSession, per_model_cache: PerModelBreakdownCache):
        self._session = session
        self._per_model_cache = per_model_cache

    async def grant(self, *, tenant_id: str, tokens: int, note: str, granted_by: str) -> TenantBudgetCredit:
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        if not note.strip():
            raise ValueError("note must be non-empty")
        period, _ = _current_period("UTC")        # credits always bound to UTC month
        credit = TenantBudgetCredit(
            id=ulid(),
            tenant_id=tenant_id,
            period=period,
            tokens=tokens,
            note=note.strip(),
            granted_by=granted_by,
        )
        self._session.add(credit)
        await self._session.flush()
        # Invalidate per-model cache so the next breakdown query reflects the
        # newly granted credit (effective_cap changed).
        self._per_model_cache.invalidate(tenant_id, period)
        return credit

    async def sum_for_period(self, tenant_id: str, period: str) -> int:
        result = await self._session.execute(
            select(func.coalesce(func.sum(TenantBudgetCredit.tokens), 0))
            .where(TenantBudgetCredit.tenant_id == tenant_id)
            .where(TenantBudgetCredit.period == period)
        )
        return int(result.scalar_one())
```

### 3.4 Admin API (`apps/api/src/admin/api.py`)

```python
@router.post(
    "/tenants/{tenant_id}/credits",
    status_code=201,
    response_model=CreditResponse,
)
async def grant_credit(
    tenant_id: str,
    body: CreditRequest,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> CreditResponse:
    # Super-admin only — same anti-enumeration pattern as /budget/cleanup
    # (Pack A #1). Per-tenant admin tokens have tenant_id set; super-admin
    # tokens have tenant_id=None (via create_access_token's extra override).
    if claims.get("tenant_id") is not None:
        raise HTTPException(status_code=404, detail="not found")
    if not await _tenant_exists(tenant_id):
        raise HTTPException(status_code=404, detail="tenant not found")
    sm = get_sessionmaker()
    async with sm() as session:
        svc = CreditService(session, get_per_model_cache())
        credit = await svc.grant(
            tenant_id=tenant_id,
            tokens=body.tokens,
            note=body.note,
            granted_by=claims["sub"],
        )
        await session.commit()
    return CreditResponse.model_validate(credit)


@router.get(
    "/tenants/{tenant_id}/credits",
    response_model=CreditListResponse,
)
async def list_credits(
    tenant_id: str,
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    claims: Annotated[dict[str, Any], Depends(require_admin)] = None,
) -> CreditListResponse:
    # Super-admin only — same as POST above.
    if claims.get("tenant_id") is not None:
        raise HTTPException(status_code=404, detail="not found")
    if not await _tenant_exists(tenant_id):
        raise HTTPException(status_code=404, detail="tenant not found")
    sm = get_sessionmaker()
    async with sm() as session:
        svc = CreditService(session, get_per_model_cache())
        rows = await svc.list_for_period(tenant_id, period)
        total = await svc.sum_for_period(tenant_id, period)
    return CreditListResponse(
        credits=[CreditResponse.model_validate(r) for r in rows],
        total_tokens=total,
    )
```

Anti-enumeration mirrors Pack A's `/budget/cleanup` pattern: per-tenant admin tokens (which always have `tenant_id` set to their own tenant) get HTTP 404 on these endpoints. Only super-admin tokens (with `tenant_id=None` via `create_access_token`'s `extra` override) can grant or list credits.

### 3.5 Schemas (`apps/api/src/admin/schemas/budget.py`)

```python
class CreditRequest(BaseModel):
    tokens: int = Field(..., gt=0)
    note: str = Field(..., min_length=1, max_length=500)


class CreditResponse(BaseModel):
    id: str
    tenant_id: str
    period: str
    tokens: int
    note: str
    granted_by: str
    created_at: datetime

    class Config:
        from_attributes = True


class CreditListResponse(BaseModel):
    credits: list[CreditResponse]
    total_tokens: int
```

### 3.6 Resolver integration (`apps/api/src/budget/resolver.py`)

New helper:

```python
async def _compute_effective_cap(self, period: str) -> int:
    base = self._budget.hard_cap_tokens or 0
    if self._credit_service is None:
        return base
    return base + await self._credit_service.sum_for_period(self._tenant_id, period)
```

`_pre_check` and `_post_record` both call `_compute_effective_cap(period)` instead of using `self._budget.hard_cap_tokens` directly for the comparison. Constructor signature extends to accept optional `credit_service` parameter (defaults to `None` for backward compat with existing Pack A tests).

`CreditService` instance is created lazily inside `ainvoke` from a session, OR injected via constructor if a session-aware service is desired. Pack B takes the lazy approach (consistent with how `SnapshotRepository` is constructed today).

### 3.7 Reconcile semantics

Effective cap auto-recomputes on next `_pre_check` because `sum_for_period` is fresh. **No manual reconcile endpoint** — the credit insert + next pre-check IS the reconcile. (The original option of "POST credits + reconcile" was selected because the reconcile step is implicit and atomic with the insert; no second endpoint needed.)

### 3.8 Tests for #2

| Test | Asserts |
|------|---------|
| `test_credit_grant_inserts_row.py` | Row exists with period=YYYY-MM, tokens, note, granted_by=user_id |
| `test_credit_grant_validates_positive_tokens.py` | `tokens=0` → ValueError; `tokens=-1` → ValueError |
| `test_credit_grant_validates_note.py` | empty/whitespace `note` → ValueError |
| `test_credit_sum_for_period.py` | Multi-month isolation: SUM only includes rows for queried period |
| `test_effective_cap_includes_credits.py` | Resolver with `credit_service` mock returns `base + sum` |
| `test_effective_cap_falls_back_when_no_credit_service.py` | Without `credit_service`, returns `base` (backward compat) |
| `test_credit_api_super_admin_only.py` | Per-tenant admin → 404; super_admin → 201 |
| `test_credit_api_anti_enumeration.py` | Per-tenant admin probing another tenant_id → 404 (not 403) |
| `test_credit_list_endpoint.py` | GET returns credits array + total_tokens |

---

## 4. Task 2 — #5 Per-Model Breakdown (read-only)

### 4.1 No new table

Per-model usage is computed on demand from `llm_usage`. New cache (not a table).

### 4.2 PerModelBreakdownCache (`apps/api/src/budget/per_model.py`)

```python
@dataclass
class ModelUsage:
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    request_count: int


class PerModelBreakdownCache:
    def __init__(self, ttl_seconds: int = 30):
        self._ttl = ttl_seconds
        self._store: dict[tuple[str, str], tuple[float, list[ModelUsage]]] = {}

    def get(self, tenant_id: str, period: str) -> list[ModelUsage] | None:
        key = (tenant_id, period)
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, data = entry
        if time.monotonic() - ts > self._ttl:
            del self._store[key]
            return None
        return data

    def set(self, tenant_id: str, period: str, data: list[ModelUsage]) -> None:
        self._store[(tenant_id, period)] = (time.monotonic(), data)

    def invalidate(self, tenant_id: str, period: str) -> None:
        self._store.pop((tenant_id, period), None)
```

**TTL default 30s**, configurable via `TENANT_BUDGET_PER_MODEL_CACHE_TTL_SECONDS`.

### 4.3 PerModelService

```python
class PerModelService:
    def __init__(self, session: AsyncSession, cache: PerModelBreakdownCache):
        self._session = session
        self._cache = cache

    async def get_breakdown(self, tenant_id: str, period: str) -> list[ModelUsage]:
        cached = self._cache.get(tenant_id, period)
        if cached is not None:
            return cached
        period_start = datetime.strptime(period + "-01", "%Y-%m-%d")
        rows = await self._session.execute(
            select(
                LLMUsage.provider,
                LLMUsage.model,
                func.sum(LLMUsage.prompt_tokens),
                func.sum(LLMUsage.completion_tokens),
                func.count(),
            )
            .where(LLMUsage.tenant_id == tenant_id)
            .where(LLMUsage.created_at >= period_start)
            .where(LLMUsage.cached == False)  # only billable calls
            .group_by(LLMUsage.provider, LLMUsage.model)
        )
        result = [
            ModelUsage(
                provider=row.provider,
                model=row.model,
                prompt_tokens=int(row.prompt or 0),
                completion_tokens=int(row.completion or 0),
                total_tokens=int(row.prompt or 0) + int(row.completion or 0),
                request_count=int(row.cnt),
            )
            for row in rows
        ]
        self._cache.set(tenant_id, period, result)
        return result
```

Cache invalidation triggers:

1. `BudgetResolver._post_record` (after each successful usage write) — pass `per_model_cache` into the resolver and call `invalidate(tenant_id, period)`.
2. `CreditService.grant` (already shown in §3.3) — credit grant changes effective cap, breakdown may be re-queried by admin.

### 4.4 Snapshot endpoint extension

Existing Pack A endpoint:

```
GET /api/v1/admin/tenants/{tenant_id}/budget/usage
```

Extends with optional `?breakdown=true` query param:

```
GET /api/v1/admin/tenants/{tenant_id}/budget/usage?breakdown=true
```

Auth (unchanged from Pack A): per-tenant admin sees own tenant; cross-tenant → 404. Super-admin tokens (tenant_id=None) ALSO see 404 against per-tenant endpoints in this codebase — pre-existing limitation that Pack B does NOT fix (out of scope).

Response (with breakdown):

```json
{
  "tenant_id": "...",
  "period": "2026-10",
  "soft_warn_tokens": 5000,
  "hard_cap_tokens": 8000,        // base
  "effective_cap": 12000,          // base + sum(credits for current period)
  "tokens_used": 7300,
  "remaining": 4700,
  "soft_warn_fired_at": null,
  "credits_total": 4000,
  "breakdown": [
    {"provider": "openai", "model": "gpt-4o-mini",
     "prompt_tokens": 1200, "completion_tokens": 800,
     "total_tokens": 2000, "request_count": 42},
    {"provider": "anthropic", "model": "claude-haiku-4-5-20251001",
     "prompt_tokens": 3000, "completion_tokens": 2300,
     "total_tokens": 5300, "request_count": 38}
  ]
}
```

Without `?breakdown=true`, response adds `effective_cap` and `credits_total` to Pack A's existing fields but omits `breakdown`. All Pack A fields stay present and unchanged (additive only — existing JSON parsers continue to work). `effective_cap` and `credits_total` are computed from a single `SUM(tenant_budget_credits.tokens WHERE period=current_period)` query inside the snapshot fetch.

Schemas:

```python
class BreakdownItem(BaseModel):
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    request_count: int


class BudgetSnapshotResponse(BaseModel):  # extends Pack A's existing schema
    # ... existing fields ...
    effective_cap: int
    credits_total: int
    breakdown: list[BreakdownItem] | None = None
```

### 4.5 Read-only by design

Per-model data is observability only. No cap enforcement, no soft-warn logic tied to per-model, no alerting. Pure dashboard fuel for admin SPA.

### 4.6 Tests for #5

| Test | Asserts |
|------|---------|
| `test_per_model_cache_hit.py` | Second call within TTL skips DB (mock `execute` called once) |
| `test_per_model_cache_miss_after_invalidate.py` | After `invalidate`, next call re-queries |
| `test_per_model_cache_ttl_expiry.py` | After TTL elapses, next call re-queries |
| `test_per_model_cache_invalidate_on_post_record.py` | `_post_record` invalidates cache for (tenant, period) |
| `test_per_model_breakdown_endpoint_with_breakdown_true.py` | Response includes breakdown array with sums |
| `test_per_model_breakdown_endpoint_without_breakdown.py` | Response excludes breakdown array (backward compat) |
| `test_per_model_invalidate_on_credit_grant.py` | `CreditService.grant` calls cache.invalidate |
| `test_per_model_anti_enumeration.py` | Per-tenant admin → 404 |
| `test_per_model_excludes_cached_rows.py` | `LLMUsage.cached == True` rows excluded from breakdown |

---

## 5. Task 3 — #8 Provider 429 Budget Gate

### 5.1 New exception (`apps/api/src/budget/exceptions.py`)

```python
class TenantBudgetRateLimited(Exception):
    """Raised when remaining budget < threshold AND inner chain returned 429.
    Signals outer code: tenant has hit a budget wall while providers are still
    rate-limiting. Do not retry; surface as HTTP 429 to caller.
    """

    def __init__(self, *, tenant_id: str, period: str, remaining: int, threshold: int):
        self.tenant_id = tenant_id
        self.period = period
        self.remaining = remaining
        self.threshold = threshold
        super().__init__(
            f"tenant {tenant_id} budget low in {period}: "
            f"remaining={remaining} < threshold={threshold} after 429"
        )
```

### 5.2 Resolver changes (`apps/api/src/budget/resolver.py`)

Add imports for `RateLimited` (from `llm_client.exceptions`) and `TenantBudgetRateLimited` (from `budget.exceptions`). `ainvoke` adds a try/except around the inner call:

```python
from llm_client.exceptions import RateLimited, TenantBudgetExceeded
from budget.exceptions import TenantBudgetRateLimited

async def ainvoke(self, request: ChatRequest) -> ChatResponse:
    period, _ = _current_period(self._budget.period_anchor_tz)
    snap, _ = await self._ensure_snapshot(period)
    effective_cap = await self._compute_effective_cap(period)
    if snap.tokens_used >= effective_cap:
        raise TenantBudgetExceeded(...)

    try:
        resp = await self._inner.ainvoke(request)
    except RateLimited:
        # 429 — provider rejected before token consumption.
        # Invalidate per-model cache (the request did hit a provider).
        # Do NOT post_record (no tokens billed).
        self._per_model_cache.invalidate(self._tenant_id, period)
        await self._maybe_raise_budget_gate(period)
        raise  # gate didn't fire → propagate 429

    await self._post_record(resp=resp, period=period)
    self._per_model_cache.invalidate(self._tenant_id, period)
    return resp


async def _maybe_raise_budget_gate(self, period: str) -> None:
    snap, _ = await self._ensure_snapshot(period)
    effective_cap = await self._compute_effective_cap(period)
    remaining = effective_cap - snap.tokens_used
    threshold = settings.tenant_budget_429_skip_threshold_tokens
    if remaining < threshold:
        LLM_BUDGET_GATE_TOTAL.inc()
        raise TenantBudgetRateLimited(
            tenant_id=self._tenant_id,
            period=period,
            remaining=remaining,
            threshold=threshold,
        )
```

`_compute_effective_cap` is the same helper from §3.6. `_per_model_cache` is the new dependency injected into the resolver constructor (optional, defaults to a no-op cache for backward compat in Pack A's existing tests).

### 5.3 New metric (`apps/api/src/core/business_metrics.py`)

```python
from prometheus_client import Counter

LLM_BUDGET_GATE_TOTAL = Counter(
    "lumen_llm_budget_gate_total",
    "Number of times BudgetResolver raised TenantBudgetRateLimited after observing 429",
)
```

**Zero-label.** Counter reset on process restart (acceptable — this is a rate signal for alerting, not a cumulative total).

### 5.4 New setting (`apps/api/src/core/config.py`)

```python
tenant_budget_429_skip_threshold_tokens: int = Field(
    default=1000,
    alias="TENANT_BUDGET_429_SKIP_THRESHOLD_TOKENS",
)
tenant_budget_per_model_cache_ttl_seconds: int = Field(
    default=30,
    alias="TENANT_BUDGET_PER_MODEL_CACHE_TTL_SECONDS",
)
```

Disabling the gate: set `TENANT_BUDGET_429_SKIP_THRESHOLD_TOKENS=0` (no remaining is ever below 0 for non-negative `snap.tokens_used`, so gate never fires). Default 1000 enables it for new deployments; existing deployments opt-in via env var.

### 5.5 FastAPI exception handler

Registered in `apps/api/src/main.py` (the API app factory). No prior handler exists for `TenantBudgetExceeded` (it currently propagates as FastAPI's default 500); Pack B registers a handler **only** for the new `TenantBudgetRateLimited`:

```python
from budget.exceptions import TenantBudgetRateLimited

@app.exception_handler(TenantBudgetRateLimited)
async def _budget_rate_limited_handler(request, exc):
    return JSONResponse(
        status_code=429,
        content={
            "error": "budget_rate_limited",
            "tenant_id": exc.tenant_id,
            "period": exc.period,
            "remaining": exc.remaining,
            "threshold": exc.threshold,
        },
    )
```

(`TenantBudgetExceeded` handler registration is intentionally NOT in Pack B scope — the cap-exceeded path's behavior remains unchanged from Pack A. Adding it would be a small UX improvement but is a behavior change beyond closing tech debt items #2/#5/#8.)

### 5.6 Tests for #8

| Test | Asserts |
|------|---------|
| `test_budget_gate_raises_when_remaining_below_threshold.py` | snap at `effective_cap - 500`, mock inner raises `RateLimited` → `TenantBudgetRateLimited` |
| `test_budget_gate_does_not_raise_when_remaining_above.py` | snap at `effective_cap - 5000`, mock raises `RateLimited` → re-raised (gate did not fire) |
| `test_budget_gate_does_not_post_record_on_429.py` | After gate path, `snapshot_repo.set_tokens_used` not called |
| `test_budget_gate_invalidates_per_model_cache.py` | After 429 path, `per_model_cache.invalidate` called |
| `test_budget_gate_metric_increments.py` | `LLM_BUDGET_GATE_TOTAL` count += 1 on gate fire |
| `test_budget_gate_disabled_when_threshold_zero.py` | `TENANT_BUDGET_429_SKIP_THRESHOLD_TOKENS=0` → gate never fires |
| `test_budget_gate_uses_effective_cap_not_base.py` | With credits present, gate uses `base + credits` (not just `base`) |
| `test_budget_gate_exception_handler.py` | FastAPI returns HTTP 429 with `error=budget_rate_limited` JSON |

---

## 6. End-to-end test

Single `test_pack_b_e2e.py` covering the full flow:

1. Tenant has `hard_cap_tokens=1000`, no credits, `soft_warn_tokens=2000`.
2. 5 successful 200-token calls → `tokens_used=1000`, next call raises `TenantBudgetExceeded`.
3. Super_admin grants credit of 500 tokens → effective_cap = 1500.
4. Next 200-token call succeeds → `tokens_used=1200`.
5. Force `tokens_used=1450`, mock `RateLimited` → gate fires (remaining=50 < 1000), `TenantBudgetRateLimited` raised, HTTP 429.
6. Verify `LLM_BUDGET_GATE_TOTAL` counter incremented by 1.
7. Query `GET /admin/tenants/{tenant_id}/budget/usage?breakdown=true` → response includes 2 distinct (provider, model) entries summing to 1450 (one openai, one anthropic), `effective_cap=1500`, `credits_total=500`.

---

## 7. File map

**New files:**

```
apps/api/src/budget/credits.py            # Task 1: CreditService + helpers
apps/api/src/budget/per_model.py          # Task 2: PerModelService + PerModelBreakdownCache
apps/api/src/budget/exceptions.py         # Task 3: TenantBudgetRateLimited
apps/api/migrations/versions/19_add_tenant_budget_credits.py
apps/api/tests/budget/test_credits.py
apps/api/tests/budget/test_per_model.py
apps/api/tests/budget/test_budget_gate.py
apps/api/tests/budget/test_pack_b_e2e.py
```

**Modified files:**

```
apps/api/src/budget/models.py             # Add TenantBudgetCredit ORM
apps/api/src/budget/resolver.py           # _compute_effective_cap, _maybe_raise_budget_gate, per_model_cache dep
apps/api/src/admin/api.py                 # POST/GET credits endpoints, snapshot breakdown param, exception handler
apps/api/src/admin/schemas/budget.py      # CreditRequest, CreditResponse, BreakdownItem, extended snapshot
apps/api/src/main.py                      # Register TenantBudgetRateLimited handler
apps/api/src/core/config.py               # 2 new settings
apps/api/src/core/business_metrics.py     # LLM_BUDGET_GATE_TOTAL counter
README.md                                 # Remove #2/#5/#8 from M4.D tech debt list; add known-limitation note
```

**No changes** to:
- M4.B `FallbackResolver` — Pack B observes its 429, doesn't couple to it.
- M4.C `TenantResolver` / Fernet / cache.
- Pack A's `_pre_check` / `_post_record` / sticky soft-warn / cleanup logic — only their cap source changes (now `_compute_effective_cap`).

---

## 8. Rollout

- **Single PR, single release.** Three task-sized commits (one per item) for clean revert.
- **Migration 19** runs once at deploy. No backfill — empty table.
- **Settings:**
  - `TENANT_BUDGET_429_SKIP_THRESHOLD_TOKENS=1000` (default — enables gate)
  - `TENANT_BUDGET_PER_MODEL_CACHE_TTL_SECONDS=30` (default)
- **Backwards compat:** Without env override, all behavior changes are gated by the existing per-tenant `TenantBudget` row. Tenants without a row are unaffected.
- **Demo:** Admin SPA gains "Credits" tab (super_admin only) + per-model breakdown panel in snapshot view.

---

## 9. Out of scope (deferred)

| # | Item | Why deferred |
|---|------|--------------|
| 6 | No non-LLM cost budget | Out of Pack B scope (Pack C). |

---

## 10. Success criteria

- [ ] All 3 tech debt items closed (#2, #5, #8).
- [ ] All tests pass (9 + 9 + 8 + 1 = 27 new tests, plus existing 18 from Pack A remain green).
- [ ] Anti-enumeration preserved on all new admin endpoints.
- [ ] `LLM_BUDGET_GATE_TOTAL` exposed via `/metrics`.
- [ ] No regressions in M4.A / M4.B / M4.C / Pack A integration tests.
- [ ] README M4.D tech debt list updated to remove #2, #5, #8; only #6 remains.
