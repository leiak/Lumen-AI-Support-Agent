# M4.C — LLM Gateway Tenant Resolver (BYOK) Design

> Per-tenant Bring-Your-Own-Key for the LLM Gateway: each tenant supplies its own provider API keys via a DB-backed config table; resolvers are built per-tenant with Fernet-encrypted credentials, cached in-process. See [[2026-09-20-m4-a-gateway-core-design]] (resolver seam) and [[2026-10-04-m4-b-fallback-resolver-design]] (chain orchestrator this spec composes).

**Status:** Draft
**Author:** brainstorming session 2026-10-05
**Predecessor:** M4.B shipped at commit `404d91e`; M4.A shipped at `f1a63ae`

---

## Confirmed assumptions

These were confirmed during brainstorming and are baked into the design below. Flag any objection during spec review.

| # | Assumption | Source |
|---|------------|--------|
| A1 | BYOK scope = **API key only**. Per-tenant fallback chain is NOT in M4.C; the global `LLM_FALLBACK_CHAIN` env still drives chain configuration for every tenant. | Q1 answer: "只 API key" |
| A2 | Config storage = **independent `tenant_llm_configs` table** (one row per `(tenant_id, provider_name)`). Does NOT reuse `tenants.settings` JSON. | Q2 answer |
| A3 | API keys are encrypted with **Fernet (AES-128-CBC + HMAC)** at the application layer; ciphertext stored in DB. Master key from env (`TENANT_LLM_FERNET_KEY`). | Q3 answer |
| A4 | Config cache = **in-process LRU + TTL** (`tenant_llm_cache_ttl_s=60.0`, `tenant_llm_cache_maxsize=1024`). No Redis. No explicit invalidation. | Q4 answer |
| A5 | When tenant has no `enabled` row for the requested provider family, raise **`TenantLlmNotConfigured(ProviderUnavailable)`**. No silent fallback to global project keys. | Q5 + clarifying answer |
| A6 | `LLMClient.__init__` signature **unchanged** — `tenant_id` remains a string. `TenantResolver` is built inside `_default_llm_client_factory`, hidden behind the resolver protocol. | Q6 answer |
| A7 | A tenant can register multiple providers (`minimax` + `anthropic` + `openai`); each is an independent row. The chain still picks the primary via the prefix router. | Designer recommendation, accepted |
| A8 | `TenantLlmNotConfigured` carries the **list of provider names** the tenant is missing (so admins can diagnose from one error). | Designer recommendation, accepted |
| A9 | Admin write API (`POST /admin/tenants/{id}/llm-configs`) does NOT clear the cache. Operators see propagation within ≤ 60s (TTL window). | Designer recommendation, accepted — see tech debt #1 |
| A10 | `stream_chat()` also routes through `TenantResolver` (it shares the resolver instance, not a separate one). M4.B's "stream skips fallback" rule is preserved. | Designer recommendation, accepted |

---

## 1. Goals

- Allow each tenant to supply its own LLM provider API keys (BYOK) without code changes.
- Keep the M4.A `Resolver = Callable[[ChatRequest], BaseProvider]` protocol **unchanged** — `TenantResolver` slots in as another resolver implementation.
- Compose cleanly with M4.B's `FallbackResolver` — a tenant can have its own provider set **and** benefit from the global fallback chain (cross-provider failover within the tenant's own providers).
- Encrypt secrets at rest with Fernet (master key in env).
- Avoid per-request DB roundtrips via an in-process LRU + TTL cache.
- Add an admin API to upsert tenant provider configs without exposing decrypted keys.

## 2. Non-Goals (explicitly deferred)

- **Per-tenant fallback chain** — tenant-specific chains (independent of global `LLM_FALLBACK_CHAIN`) deferred. See tech debt #4.
- **KMS / HSM integration** — Fernet key comes from env. External KMS rotation deferred.
- **Explicit cache invalidation** — config writes rely on TTL expiry (≤ 60s propagation). Pub/sub deferred.
- **Audit log for admin writes** — who/when/what tracking of `tenant_llm_configs` rows. Deferred.
- **Per-tenant token budget / spend caps** — M4.D.
- **Dynamic key rotation** — key rotation requires service restart + re-encrypt of all rows. See tech debt #2.

## 3. Background

M4.A's resolver seam is the natural integration point: each `LLMClient` already receives a `tenant_id` (opaque string used for usage attribution). M4.B added a chain orchestrator on top of the same seam. M4.C adds **per-tenant provider configuration**: a `TenantResolver` that, given `tenant_id`, looks up that tenant's enabled API keys, decrypts them, builds a tenant-private `LLMGateway`, and exposes its default resolver. The seam contract is preserved — `TenantResolver` implements the same `Resolver` + optional `ainvoke` shape.

## 4. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│ LLMClient.chat(req)        # signature unchanged from M4.A              │
│   ├─ _default_llm_client_factory(tenant_id)   # per-request, current    │
│   │     │                                                              │
│   │     ├─ TenantResolver.build(tenant_id)                              │
│   │     │     │                                                        │
│   │     │     ├─ TenantLLMConfigCache.get_or_load(tenant_id)           │
│   │     │     │     ├─ cache hit (TTL 60s) → return cached resolvers   │
│   │     │     │     └─ cache miss:                                       │
│   │     │     │           ├─ TenantLLMConfigRepository.list_by_tenant  │
│   │     │     │           ├─ TenantLLMConfigCipher.decrypt(per row)    │
│   │     │     │           ├─ build dict[name, BaseProvider]            │
│   │     │     │           ├─ if dict is empty → raise                  │
│   │     │     │           │     TenantLlmNotConfigured(tenant_id)     │
│   │     │     │           └─ cache.put + return                        │
│   │     │     │                                                        │
│   │     │     ├─ LLMGateway(providers=tenant_providers,                │
│   │     │     │                 default_fallback_chain=parse_env(...)) │
│   │     │     │     └─ default_resolver → PrefixResolver /              │
│   │     │     │                   FallbackResolver (M4.B unchanged)    │
│   │     │     │                                                        │
│   │     │     └─ return resolver   # cache callable: Resolver          │
│   │     │                                                              │
│   │     └─ LLMClient(provider_resolver=tenant_resolver,                │
│   │                  tenant_id=tenant_id)         # M4.A signature     │
│   │                                                                  │
│   └─ M4.A retry loop / M4.B FallbackResolver.ainvoke (unchanged)     │
└──────────────────────────────────────────────────────────────────────┘
```

**Why a per-tenant `LLMGateway` instead of one shared gateway**: the providers carry tenant-private API keys (their `httpx.AsyncClient` is constructed with that key). Sharing one gateway across tenants would leak key A into tenant B's HTTP traffic. Each `TenantResolver` instance owns its `LLMGateway`; the gateway is lightweight (just a dict of `BaseProvider` references); per-request construction is acceptable — the cache absorbs the cost after the first call.

## 5. Component Changes

### 5.1 New files

| File | Purpose | Approx. lines |
|------|---------|---------------|
| `apps/api/migrations/versions/16_add_tenant_llm_configs.py` | Create `tenant_llm_configs` table | ~50 |
| `apps/api/src/llm_client/tenant_config_models.py` | `TenantLLMConfig` ORM + `TenantLLMConfigRepository` | ~120 |
| `apps/api/src/llm_client/tenant_config_crypto.py` | `TenantLLMConfigCipher` (Fernet wrapper) | ~60 |
| `apps/api/src/llm_client/tenant_resolver.py` | `TenantResolver` class + `TenantLLMConfigCache` (LRU+TTL) | ~180 |
| `apps/api/src/admin/schemas/tenant_llm_config.py` | Pydantic request/response schemas for admin API | ~50 |
| `apps/api/tests/llm_client/test_tenant_config_crypto.py` | Unit tests | ~80 |
| `apps/api/tests/llm_client/test_tenant_config_models.py` | Repository tests | ~120 |
| `apps/api/tests/llm_client/test_tenant_resolver.py` | Cache + resolver tests | ~250 |
| `apps/api/tests/llm_client/integration/test_tenant_resolver_e2e.py` | HTTP e2e tests via pytest-httpx | ~150 |
| `apps/api/tests/admin/test_tenant_llm_config_api.py` | Admin API tests | ~120 |

### 5.2 Modified files

| File | Change |
|------|--------|
| `apps/api/src/llm_client/exceptions.py` | Add `TenantLlmNotConfigured(ProviderUnavailable)` carrying `tenant_id: str` + `missing_providers: list[str]` |
| `apps/api/src/agent/llm_factory.py` | `_default_llm_client_factory(tenant_id)` constructs via `TenantResolver.build(tenant_id)`. Existing fallback chain wiring preserved. |
| `apps/api/src/core/config.py` | Add `tenant_llm_fernet_key: str \| None`, `tenant_llm_cache_ttl_s: float = 60.0`, `tenant_llm_cache_maxsize: int = 1024` |
| `apps/api/src/admin/api.py` | Add `POST /admin/tenants/{tenant_id}/llm-configs` + `GET /admin/tenants/{tenant_id}/llm-configs` (no decrypted key in response) |
| `apps/api/src/admin/repository.py` | Wrap `TenantLLMConfigRepository` for admin layer (validation, tenant existence check) |
| `apps/api/src/admin/schemas/__init__.py` | Export new schemas |
| `README.md` | Add M4.C status row + 4 known tech debt items |

No frontend change. No new top-level module.

## 6. Data Model

### 6.1 `tenant_llm_configs` table

```sql
CREATE TABLE tenant_llm_configs (
    id                  VARCHAR(26) PRIMARY KEY,           -- ULID
    tenant_id           VARCHAR(26) NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider_name       VARCHAR(64) NOT NULL,              -- "minimax" | "anthropic" | "openai" | ...
    encrypted_api_key   BYTEA NOT NULL,                    -- Fernet ciphertext
    base_url            VARCHAR(512),                      -- optional override (e.g. self-hosted MiniMax)
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMP NOT NULL DEFAULT now(),
    updated_at          TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, provider_name)
);
CREATE INDEX ix_tenant_llm_configs_tenant_id ON tenant_llm_configs(tenant_id);
```

**`enabled=FALSE`** semantics: row exists but the resolver skips it. Allows admin to temporarily disable a provider without deleting the row (preserves the key for re-enable). See tech debt #5 for follow-up.

### 6.2 Ciphertext format

`Fernet.encrypt(plaintext)` produces a URL-safe base64 token. Storing as `BYTEA` keeps the column compact and avoids text-mode round-trip issues with whitespace.

## 7. Data Flow

### 7.1 Per-call (chat)

```
_default_llm_client_factory(tenant_id)
  │
  ├─ TenantResolver.build(tenant_id)
  │     │
  │     ├─ cache.get(tenant_id)        ← _tenant_llm_config_cache
  │     │     └─ hit & not expired → return cached (resolver, providers)
  │     │
  │     ├─ miss / expired:
  │     │     │
  │     │     ├─ TenantLLMConfigRepository.list_by_tenant(tenant_id)
  │     │     │     └─ SELECT WHERE tenant_id=$1 AND enabled=TRUE
  │     │     │
  │     │     ├─ for row in rows:
  │     │     │     api_key = TenantLLMConfigCipher.decrypt(row.encrypted_api_key)
  │     │     │     if decrypt fails → RuntimeError ("encrypted_api_key corrupted")
  │     │     │     provider = _build_provider(row.provider_name, api_key, row.base_url)
  │     │     │
  │     │     ├─ if not providers → raise TenantLlmNotConfigured(tenant_id, missing=known_providers)
  │     │     │
  │     │     ├─ gateway = LLMGateway(
  │     │     │       providers=providers,
  │     │     │       default_fallback_chain=parse_fallback_chain_env(settings.llm_fallback_chain),
  │     │     │       attempt_timeout_s=settings.llm_fallback_attempt_timeout_s)
  │     │     │
  │     │     └─ cache.put(tenant_id, (gateway.default_resolver, providers))
  │     │
  │     └─ return gateway.default_resolver
  │
  └─ LLMClient(provider_resolver=resolver, tenant_id=tenant_id)   ← M4.A signature

LLMClient.chat(req)
  └─ hasattr(resolver, "ainvoke") → resolver.ainvoke(req)        ← M4.B
                                  OR existing retry loop           ← M4.A
```

**`_build_provider`** is a thin factory:

```python
def _build_provider(name: str, api_key: str, base_url: str | None) -> "BaseProvider":
    if name == "minimax":
        return OpenAIProvider(
            api_key=api_key,
            model=settings.minimax_model or "MiniMax-M3",
            base_url=base_url or settings.minimax_base_url or "https://api.minimaxi.com/v1",
        )
    if name == "anthropic":
        return AnthropicProvider(
            api_key=api_key,
            model=settings.default_llm_model,   # tenant doesn't override model here
        )
    if name == "openai":
        return OpenAIProvider(
            api_key=api_key,
            model=settings.openai_model or "gpt-4o-mini",
        )
    raise ValueError(f"Unknown provider_name {name!r}")
```

The model remains project-wide for M4.C — `provider_name` controls routing; per-tenant model override is a future addition (see tech debt #6).

### 7.2 Admin write

```
POST /admin/tenants/{tenant_id}/llm-configs
  body: {"provider_name": "minimax", "api_key": "sk-...", "base_url": null}
  │
  ├─ admin middleware (auth + tenant scope check)         # M3 admin JWT
  │
  ├─ TenantRepository.get_by_id(tenant_id)                 # 404 if not found
  │
  ├─ TenantLLMConfigCipher.encrypt(plaintext)              # Fernet
  │
  ├─ INSERT … ON CONFLICT (tenant_id, provider_name) DO UPDATE
  │     SET encrypted_api_key=excluded.encrypted_api_key,
  │         base_url=excluded.base_url,
  │         updated_at=now()
  │
  └─ return 201 {"provider_name": "...", "enabled": true, "updated_at": "..."}
     # NEVER return encrypted_api_key or api_key
```

### 7.3 Admin read

```
GET /admin/tenants/{tenant_id}/llm-configs
  │
  ├─ admin middleware
  │
  ├─ SELECT provider_name, base_url, enabled, created_at, updated_at
  │     FROM tenant_llm_configs WHERE tenant_id=$1
  │
  └─ return [{provider_name, base_url, enabled, created_at, updated_at}, ...]
     # NEVER return encrypted_api_key / api_key
```

## 8. Behavior Details

### 8.1 TenantResolver's `Resolver` contract

`TenantResolver` exposes both `__call__(req)` and `ainvoke(req)`:

- `__call__(req)` returns the **primary provider** (gateway's prefix-routed result). This satisfies the M4.A `Resolver` protocol used by `LLMClient._resolve_request` and `stream_chat`.
- `ainvoke(req)` delegates to the inner `FallbackResolver.ainvoke` if the tenant's chain has ≥ 2 steps; otherwise runs the prefix-routed provider directly. This keeps M4.B's `LLMClient.hasattr(resolver, "ainvoke")` branch working.

```python
class _NoChainConfigured(Exception):
    """Sentinel: tenant has only one provider, no fallback chain."""

class TenantResolver:
    def __init__(self, *, gateway: LLMGateway) -> None:
        self._gateway = gateway
        self._delegate = gateway.default_resolver

    def __call__(self, request: "ChatRequest") -> "BaseProvider":
        return self._delegate(request)

    async def ainvoke(self, request: "ChatRequest") -> "ChatResponse":
        if hasattr(self._delegate, "ainvoke"):
            return await self._delegate.ainvoke(request)
        # Single-provider tenant — no chain. Signal LLMClient to fall back
        # to its own retry loop (which still works on this single provider).
        raise _NoChainConfigured()
```

`_NoChainConfigured` is a private sentinel (in `tenant_resolver.py`) caught by `LLMClient.chat()` to fall back to its own retry loop (so the metric path stays identical). This keeps the contract clean: `TenantResolver` always exposes `ainvoke`; the body decides whether to delegate to a chain or hand control back to the client.

### 8.2 Cache semantics

```python
class TenantLLMConfigCache:
    def __init__(self, *, ttl_s: float, maxsize: int) -> None: ...
    def get(self, tenant_id: str) -> Resolver | None: ...
    def put(self, tenant_id: str, resolver: Resolver) -> None: ...
    def invalidate(self, tenant_id: str) -> None: ...   # forward — see tech debt #1
    def clear(self) -> None: ...
```

LRU eviction (`OrderedDict` + move-to-end on access) capped at `maxsize`. TTL checked on `get` — expired entries return `None` and are evicted lazily.

**Decryption cost**: each cache miss costs ~1 DB roundtrip + N decrypts + N provider constructions (~10ms for N=2). The 60s TTL keeps this amortized to < 1ms per call once warm. Cache is per-process; multi-instance deployments see DB hit once per process per tenant per 60s.

### 8.3 Fernet cipher

```python
class TenantLLMConfigCipher:
    def __init__(self, master_key: str | None) -> None:
        if not master_key:
            raise RuntimeError(
                "TENANT_LLM_FERNET_KEY is required for M4.C BYOK. "
                "Generate with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
            )
        self._fernet = Fernet(master_key.encode("utf-8"))

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        return self._fernet.decrypt(ciphertext).decode("utf-8")
```

A `cryptography.fernet.InvalidToken` exception during decrypt means the master key changed since the row was encrypted. Surface as `RuntimeError("encrypted_api_key corrupted: master key rotation required")` — operators must re-write the row. See tech debt #2.

### 8.4 Strict-mode behavior

**Strict boundary** is per-call only:

| Tenant state | LLM call outcome |
|---|---|
| Tenant has enabled key(s) for ≥ 1 provider | Normal — chain resolves via M4.A/B |
| Tenant has zero enabled keys | `TenantLlmNotConfigured` raised |
| Tenant has key for `minimax` but request uses `claude-*` (no anthropic key) | `TenantLlmNotConfigured` with `missing_providers=["anthropic"]` |
| `tenants` row doesn't exist | factory raises `ValueError("unknown tenant")` — caller (API endpoint) returns 404 |

Demo / staging seeding: admins use the admin API to seed `tenant_llm_configs` for the demo tenant. Without seeding, **the demo tenant cannot chat** — this is intentional. See tech debt #7 for a follow-up that allows demo mode to seed automatically from env.

### 8.5 Metric behavior

`TenantLlmNotConfigured` extends `ProviderUnavailable`, so `LLMClient.chat()` records the call with `route_mode="auto"` (because `TenantResolver` is neither a `PinnedResolver` nor a `FallbackResolver`) and `outcome="unavailable"`, `provider="<unknown>"`. The standard failure path already captures the resolution failure at the call-site level.

For dedicated observability of "tenant not configured" (a misconfiguration rather than a transient failure), add a separate zero-label counter:

```
LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL.inc()
```

**Why no labels**: `tenant_id` as a label would explode cardinality for tenants-with-many-keys configurations and risk PII leakage via metric scraping. One series (the total count) is enough for ops alerting; tenant_id is logged at WARNING with event `llm_client.tenant_llm_not_configured` (provider name + tenant_id only, no key material).

### 8.6 Interaction with M4.B fallback chain

The tenant's `LLMGateway` is constructed with `default_fallback_chain=settings.llm_fallback_chain` (global env). When a tenant has multiple providers configured, the chain steps are built from `parse_fallback_chain_env(settings.llm_fallback_chain)` filtered to only providers the tenant has configured. If the chain references a provider the tenant doesn't own, the gateway logs `llm_client.fallback_step_skipped` (existing M4.B warning) and drops the step.

**Cross-tenant fallback**: not in scope. A tenant's providers are private; a chain step that references a provider the tenant lacks is dropped.

## 9. Testing Strategy

### 9.1 Unit tests — `tests/llm_client/test_tenant_config_crypto.py` (4 tests)

1. `test_encrypt_decrypt_roundtrip` — encrypt("sk-test"), decrypt → "sk-test".
2. `test_decrypt_rejects_wrong_key` — generate two Fernet keys; encrypt with A, decrypt with B → `InvalidToken`.
3. `test_cipher_constructor_requires_key` — `TenantLLMConfigCipher(None)` → `RuntimeError`.
4. `test_encrypted_bytes_are_url_safe_base64` — ciphertext is `bytes` and decodes cleanly.

### 9.2 Unit tests — `tests/llm_client/test_tenant_config_models.py` (5 tests)

5. `test_create_and_get` — INSERT + SELECT round-trip; `encrypted_api_key` bytes preserved.
6. `test_unique_constraint_per_tenant_provider` — duplicate `(tenant_id, provider_name)` → `IntegrityError`.
7. `test_list_by_tenant_returns_only_enabled` — enabled=FALSE rows excluded.
8. `test_list_by_tenant_returns_empty_for_new_tenant` — empty list, no exception.
9. `test_cascade_delete_with_tenant` — `DELETE FROM tenants` cascades.

### 9.3 Unit tests — `tests/llm_client/test_tenant_resolver.py` (8 tests)

10. `test_cache_hit_skips_db_lookup` — cache primed; resolver call doesn't touch DB (mocked repo `assert_not_called`).
11. `test_cache_miss_loads_from_db` — first call → 1 DB hit.
12. `test_cache_respects_ttl` — prime cache, advance clock past TTL, next call → 1 DB hit.
13. `test_cache_lru_evicts_oldest` — fill cache to `maxsize+1`; oldest entry evicted (verify DB).
15. `test_raises_tenant_llm_not_configured_on_empty` — tenant with no rows → `TenantLlmNotConfigured` raised.
16. `test_tenant_llm_not_configured_carries_missing_providers` — exception lists `["anthropic", "openai"]`.
17. `test_passes_global_fallback_chain_to_inner_gateway` — when env sets a 2-step chain, the inner gateway has a `FallbackResolver` with `ainvoke`.
18. `test_single_provider_tenant_returns_resolver_without_ainvoke` — env unset → `_PrefixResolver`; `TenantResolver.ainvoke` raises `_NoChainConfigured`.

### 9.4 Integration tests — `tests/llm_client/integration/test_tenant_resolver_e2e.py` (4 tests)

19. `test_e2e_tenant_key_used_in_anthropic_request` — tenant with anthropic key; mock `https://api.anthropic.com` returns 200; verify the `x-api-key` header equals the tenant's decrypted key.
20. `test_e2e_tenant_not_configured_returns_error` — fresh tenant, no rows; `LLMClient(tenant_id).chat()` raises `TenantLlmNotConfigured`.
21. `test_e2e_tenant_fallback_chain_uses_secondary` — tenant has minimax + anthropic; env's chain = `[("minimax", …), ("anthropic", …)]`; minimax mock 503 → anthropic mock 200; verify response came from anthropic.
22. `test_e2e_tenant_key_isolation` — two tenants each with their own anthropic key; `LLMClient(tenant_id="t1").chat()` uses t1's key (verify mock receives t1's key in header); `LLMClient(tenant_id="t2").chat()` uses t2's key (no cross-leak).

### 9.5 Admin API tests — `tests/admin/test_tenant_llm_config_api.py` (3 tests)

23. `test_post_creates_row_with_encrypted_key` — POST `/admin/tenants/{id}/llm-configs` with `{"provider_name": "minimax", "api_key": "sk-..."}`; verify DB row has `encrypted_api_key != "sk-..."` (bytes) and response does NOT include `api_key`.
24. `test_post_upserts_existing_row` — second POST same provider replaces ciphertext.
25. `test_get_returns_provider_names_without_keys` — GET response is `[{provider_name, enabled, ...}]` with no `api_key` or `encrypted_api_key`.

### 9.6 Existing tests

All M4.A 46 tests, M4.B 36 new tests, M2.B/M1/etc. all stay green. The only test that needs updating is `tests/agent/integration/conftest.py`'s `_make_llm_client(tenant_id)` fixture — if it currently uses `_default_llm_client_factory` directly, no change. If it pre-builds providers, it must now seed a `tenant_llm_configs` row first.

## 10. Rollout

1. Spec commits
2. Plan via `superpowers:writing-plans` skill, split into ~5 subagent tasks
3. Implementation — subagent-driven, two-stage review per task
4. Tests — unit + admin + integration green before merge
5. README update — add M4.C status row + 4 known tech debt items
6. Memory file `m4-c-progress.md` + MEMORY.md pointer

No frontend change. Feature flag = `TENANT_LLM_FERNET_KEY` env (empty = M4.B behavior preserved).

## 11. Known Tech Debt (post-M4.C)

These will go into README's `### M4.C —` section:

1. **No explicit cache invalidation on admin write** — admin `POST /admin/tenants/{id}/llm-configs` does NOT clear the cache. Operators see propagation within ≤ 60s (TTL window). Follow-up: add Redis pub/sub (`admin.channel:tenant_llm_changed` → processes subscribe and `cache.invalidate(tenant_id)`).
2. **Fernet key rotation** — master key is loaded once at startup. Rotation requires (a) restart, (b) re-encrypt every row with the new key, (c) update env. No zero-downtime rotation.
3. **No audit log** — `tenant_llm_configs` updates are silent. Operators can't answer "who changed tenant X's key at 03:00 UTC?". Follow-up: add `tenant_llm_configs_audit` table or hook into `outbox_events`.
4. **No per-tenant fallback chain** — chain is always `LLM_FALLBACK_CHAIN` (project-wide). Tenants can't override. Out of scope for M4.C.
5. **`enabled=FALSE` semantics** — currently "skip in resolver". Could be expanded to "rate-limit" or "circuit-broken" in M4.D.
6. **No per-tenant model override** — `provider_name` controls routing, but `model` is project-default per provider family. Tenant can't say "use `claude-3-5-haiku` even though we have a `claude-3-5-sonnet` key".
7. **Demo / staging seeding** — demo tenant must be seeded via admin API before any chat works. Follow-up: support `seed_tenant_llm_from_env` that materializes `tenant_llm_configs` from `TENANT_<NAME>_API_KEY` env vars at startup, gated by `Environment.DEVELOPMENT`.

## 12. Cross-References

- M4.A design: [[2026-09-20-m4-a-gateway-core-design]] — `Resolver` seam and `LLMClient` signature.
- M4.B design: [[2026-10-04-m4-b-fallback-resolver-design]] — `FallbackResolver` + `ainvoke` extension; M4.C composes.
- M4.D (budget) — `LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL` + per-tenant token budgets.
- M3 admin API — `apps/api/src/admin/api.py` — this spec adds 2 endpoints.
- Tech debt #20 (admin SPA) — no UI for tenant LLM config in this spec; admin SPA follow-up (out of M4.C).