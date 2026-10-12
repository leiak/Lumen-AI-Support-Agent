# M5.3 — Tenant LLM Config Audit Log Design

> Append-only audit log for `tenant_llm_configs` mutations. Closes README tech debt line 372 ("No audit log — `tenant_llm_configs` 更新无审计").

**Status:** Spec (brainstorming-approved)
**Date:** 2026-10-12
**Predecessor:** M5.2 at commit `5ad1a25` (per-tenant model override)
**Author:** Brainstorming session with user

---

## 1. Goals

M4.C + M5.2 let admins write per-tenant LLM configs (BYOK API key + `base_url` + `model` + `enabled`) via the admin REST surface. There is currently no way to answer "who rotated the key for tenant T at 18:30 yesterday?" or "when did the model override for tenant X change from sonnet to haiku?" — the source row only carries the latest value.

M5.3 adds a read-only audit log:

1. **Append-only history of every field change** to `tenant_llm_configs` made via the admin REST surface.
2. **Forensic value** — operators can see who changed what, when, and the SHA-256 fingerprint of the key (not the plaintext) before/after rotation.
3. **Atomicity guarantee** — a mutation and its audit row(s) commit in a single transaction. No "state changed but no audit" half-success state.
4. **Same auth shape as the rest of admin** — admin JWT, cross-tenant 404, anti-enumeration.

## 2. Non-Goals

- **No replay/restore** — there is no "restore tenant X to T 时刻" endpoint. M5.3 is read-only history. (Operator can run a manual UPDATE if a rollback is needed.)
- **No automatic retention cleanup** — rows are forever. Operators run a manual `DELETE FROM tenant_llm_config_audit_log WHERE changed_at < now() - interval '90 days'` if they want to trim. (No `run_audit_cleanup` worker in M5.3 scope.)
- **No audit for non-admin sources** — the seed script (`extend-seed.py`) and any future system/bulk paths do NOT write audit rows in M5.3. The wrapper only instruments the admin wrapper, and only `claims["sub"]` is recorded.
- **No user-name join in audit response** — `changed_by` is the user ULID; the SPA joins against `GET /admin/tenants/{id}/users` if it needs a display name.
- **No CSV/JSON export** — JSON via the GET endpoint only.
- **No per-tenant rate limit on the GET** — same as `list_tenant_tickets` today; if needed, follow-up.
- **No PII in logs beyond the SHA-256 prefix** — operator-emitted structlog events carry only the first 16 chars of the hash (enough to correlate, not enough to reverse).

## 3. Background

`tenant_llm_configs` (introduced M4.C, extended M5.2) is a per-(tenant, provider) row:

```
id, tenant_id, provider_name, encrypted_api_key, base_url, model, enabled, created_at, updated_at
```

The four admin mutation endpoints in `apps/api/src/admin/api.py` (lines 658-861) wrap the inner `TenantLLMConfigRepository` (`apps/api/src/llm_client/tenant_config_models.py`):

| Endpoint | Inner repo call |
|---|---|
| `POST /admin/tenants/{id}/llm-configs` | `TenantLLMConfigRepository().upsert(...)` |
| `PATCH /admin/tenants/{id}/llm-configs/{provider}` | `TenantLLMConfigRepository().update(...)` |
| `DELETE /admin/tenants/{id}/llm-configs/{provider}` | `TenantLLMConfigRepository().delete_by_tenant_provider(...)` |
| `GET /admin/tenants/{id}/llm-configs` | `TenantLLMConfigRepository().list_by_tenant(...)` |

The inner repo's mutating methods own their own session and call `await session.commit()` internally. This means the admin wrapper currently cannot share a session across mutation + side-effect writes — and that's the architectural obstacle for atomic audit-write.

### Closest existing pattern: `tenant_budget_credits` (M4.D Pack B)

`apps/api/src/budget/credits.py` already implements immutable audit-style rows for super-admin credit grants. Each row is append-only; the app never UPDATEs or DELETEs them. Effective budget is `SUM(tenant_budget_credits.tokens WHERE period = P)`. M5.3 follows the same shape — a separate `tenant_llm_config_audit_log` table with one row per **field change** (finer granularity than `tenant_budget_credits`, which is one row per grant event).

### Inner repo callers

The signature change to make `upsert/update/delete_by_tenant_provider` accept an optional `session: AsyncSession | None = None` is back-compat because:

- `apps/api/src/llm_client/tenant_config_models.py:134` (`upsert`) and similar: change to take `session=None`, when None open own session + commit (today's behavior).
- `AdminTenantLLMConfigRepository` (M5.3 target): pass its own session; wrapper commits at the end.
- `tests/e2e/scripts/extend-seed.py:230-237` (demo seed): no `session` arg → falls into the None branch → behavior unchanged.
- All existing test files: same — None default preserves today.

## 4. Architecture

### 4.1 Component layout

```
admin/api.py  (POST/PATCH/DELETE routes)
   │
   ├─► claims["sub"]   →  changed_by
   ├─► sha256(payload.api_key)  →  api_key_sha256  (POST only)
   │
   └─► AdminTenantLLMConfigRepository
         │   (admin/repository.py — wrapper layer)
         │   changed_by: str  (required)
         │   api_key_sha256: str | None  (POST only)
         │
         ├─ open AsyncSession (NEW: wrapper owns the session)
         ├─ tenant existence check
         ├─ old_row = inner.get_by_tenant_provider(session, ...)
         ├─ inner.upsert / update / delete  (with session=..., no commit)
         ├─ diffs = _diff_fields(old, payload, changed_by, api_key_sha256)
         ├─ audit_repo.insert_many(session, [audit rows for each diff])
         └─ session.commit()      ← single transaction
```

### 4.2 Why audit in the wrapper, not the route

Three options were considered (see brainstorming conversation 2026-10-12):

- **A. Route-level writes** — mutation and audit are two transactions, race window between them.
- **B. Wrapper-level writes (chosen)** — one transaction; wrapper already owns the encryption boundary, symmetric to also owning the audit boundary.
- **C. New `TenantLLMConfigAuditService` layer** — clean SRP but introduces a new layer the rest of the llm-configs admin surface doesn't have; YAGNI.

**Atomicity is the deciding constraint**: "mutation without audit" or "audit without mutation" is worse than no audit. Option B is the only one that delivers single-transaction atomicity without a new layer.

### 4.3 Why inner repo signature change is the right move

To share a session across `old_row fetch → mutation → audit INSERT`, the inner repo's mutating methods must not commit. Today they do. Changing their signature to `session: AsyncSession | None = None` (back-compat: when None, open own session and commit) is the minimum-blast-radius move. All four non-test callers either pass a session (M5.3 wrapper) or fall into the None branch (seed script, existing tests).

## 5. Data Model

### 5.1 Table: `tenant_llm_config_audit_log`

```sql
CREATE TABLE tenant_llm_config_audit_log (
    id              VARCHAR(26)  PRIMARY KEY,                       -- ULID
    tenant_id       VARCHAR(26)  NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider_name   VARCHAR(64)  NOT NULL,                          -- 'anthropic' / 'openai' / 'minimax'
    field           VARCHAR(32)  NOT NULL,                          -- see §5.2
    old_value       TEXT,                                           -- NULL allowed (e.g. first-time insert for non-key fields)
    new_value       TEXT,                                           -- NULL allowed (e.g. cleared, or __deleted__)
    changed_by      VARCHAR(64)  NOT NULL,                          -- claims['sub'] (M5.3 admin path only)
    changed_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX ix_audit_log_tenant_changed_at
    ON tenant_llm_config_audit_log (tenant_id, changed_at DESC);

CREATE INDEX ix_audit_log_tenant_provider_changed_at
    ON tenant_llm_config_audit_log (tenant_id, provider_name, changed_at DESC);
```

### 5.2 `field` column values

| `field` value | Trigger | `old_value` | `new_value` | When written |
|---|---|---|---|---|
| `api_key` | POST (insert or update) | `sha256:initial` (insert) or `sha256:<old hex>` (update) | `sha256:<new hex>` | Always on POST |
| `api_key` | PATCH | — | — | Never (PATCH does not rotate key) |
| `api_key` | DELETE | — | — | Never (cannot hash old plaintext) |
| `base_url` | POST or PATCH | old or `NULL` | new or `NULL` | When value changed AND field was in payload (POST: always in payload) |
| `model` | POST or PATCH | old or `NULL` | new or `NULL` | Same |
| `enabled` | POST or PATCH | `"true"` or `"false"` | `"true"` or `"false"` | Same |
| `__deleted__` | DELETE | provider_name | `NULL` | Always on DELETE of existing row |

`field` is `VARCHAR(32) NOT NULL` at the DB layer; Pydantic `Literal[...]` validates at the ORM `__init__` boundary so typos never reach the DB.

### 5.3 No new metrics

M5.3 does not add a Prometheus counter for audit-write counts. Operators can monitor via:
- `SELECT count(*) FROM tenant_llm_config_audit_log` (row count over time)
- The structlog `audit.llm_config.changed` events (one INFO per mutation)
- `lumen_llm_config_audit_log_total` if a follow-up wants it — deferred.

## 6. Components

### 6.1 New files

- `apps/api/migrations/versions/23_tenant_llm_config_audit_log.py` — alembic migration
- `apps/api/src/llm_client/tenant_config_audit_models.py` — `TenantLLMConfigAuditLog` ORM + `TenantLLMConfigAuditRepository`
- `apps/api/src/admin/schemas/tenant_llm_config_audit.py` — `TenantLLMConfigAuditEntry`, `TenantLLMConfigAuditListResponse`
- `apps/api/tests/llm_client/test_tenant_config_audit_models.py` — ~8 tests
- `apps/api/tests/admin/test_admin_llm_config_diff.py` — ~9 diff helper unit tests (no DB)
- `apps/api/tests/admin/test_admin_llm_config_audit_integration.py` — ~12 wrapper integration tests
- `apps/api/tests/admin/test_tenant_llm_config_audit_api.py` — ~10 GET endpoint tests
- `tests/e2e/scripts/test_tenant_llm_config_audit_e2e.py` — 7-step E2E script

### 6.2 Modified files

- `apps/api/src/llm_client/tenant_config_models.py` — `upsert`, `update`, `delete_by_tenant_provider` gain `session: AsyncSession | None = None`; when None, retain current self-managed session+commit behavior.
- `apps/api/src/admin/repository.py` — `AdminTenantLLMConfigRepository.upsert/update/delete` each gain `changed_by: str` (required). `upsert` gains `api_key_sha256: str | None = None`. Internal `_diff_fields()` private function. Wrapper now opens and commits its own session.
- `apps/api/src/admin/api.py` — POST/PATCH/DELETE routes thread `claims["sub"]` and (for POST) compute `sha256(payload.api_key.encode("utf-8")).hexdigest()`. New `GET /admin/tenants/{id}/llm-configs/audit` route.
- `tests/e2e/scripts/explore.py` — Round 11 added.
- `README.md` — row 30 added; tech debt #16 marked DONE.
- `memory/2026-10-12-m5-3-progress.md` — memory entry (out-of-repo).

### 6.3 ORM signature

```python
class TenantLLMConfigAuditLog(Base):
    __tablename__ = "tenant_llm_config_audit_log"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    field: Mapped[str] = mapped_column(String(32), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
```

### 6.4 AuditRepository public methods

```python
class TenantLLMConfigAuditRepository:
    async def insert_many(
        self, session: AsyncSession, rows: list[TenantLLMConfigAuditLog],
    ) -> None:
        """Batch insert. Caller owns the transaction (no commit)."""

    async def list_by_tenant(
        self,
        tenant_id: str,
        *,
        provider_name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TenantLLMConfigAuditLog]:
        """Newest-first. Optional provider_name filter. Caller-managed session."""

    async def count_by_tenant(
        self,
        tenant_id: str,
        *,
        provider_name: str | None = None,
    ) -> int:
        """Total rows for the same WHERE clause as list_by_tenant.
        Used to populate the ``{entries, total}`` envelope."""
```

### 6.5 Diff helper

The diff helper handles POST and PATCH; DELETE bypasses the helper and writes
the `__deleted__` row directly from the wrapper (since the diff is trivial —
always one row, no comparison needed).

```python
def _diff_upsert(
    *,
    old: TenantLLMConfig | None,
    payload_model_fields_set: set[str] | None,
    new_base_url: str | None,
    new_model: str | None,
    new_enabled: bool | None,
    new_api_key_sha256: str | None,
) -> list[tuple[str, str | None, str | None]]:
    """Produce list of (field, old_repr, new_repr) audit tuples.

    Behavior:
    - POST:  payload_model_fields_set=None → diff all fields vs old
             (old is None on insert, so old_value for every field is None;
             api_key.old_value is the literal string "sha256:initial").
    - PATCH: payload_model_fields_set=set of fields in payload → only those
             fields. A field is included in the diff ONLY if its new value
             differs from the old value in the DB row.
    - DELETE: not handled here. Wrapper writes ('__deleted__', provider, None)
              directly without calling this helper.
    """
```

## 7. API Surface

### 7.1 New: GET audit endpoint

```
GET /api/v1/admin/tenants/{tenant_id}/llm-configs/audit
  ?provider_name=anthropic      (optional Literal; 422 if not in allowlist or empty)
  &limit=50                      (1..200, default 50)
  &offset=0                      (>=0, default 0)

→ 200 {
    "entries": [
      {
        "id": "01HZ...",
        "provider_name": "anthropic",
        "field": "model",
        "old_value": "claude-3-5-sonnet-20241022",
        "new_value": "claude-haiku-4-5",
        "changed_by": "01HZUSER...",
        "changed_at": "2026-10-12T18:30:00+00:00"
      },
      ...
    ],
    "total": 137
  }

→ 401  Missing/invalid bearer (require_admin)
→ 403  Non-admin role (require_admin)
→ 404  Cross-tenant OR unknown tenant (anti-enumeration, same code either way)
→ 422  Invalid provider_name / empty provider_name / limit or offset out of range
```

### 7.2 Response schemas

```python
class TenantLLMConfigAuditEntry(BaseModel):
    id: str
    provider_name: str
    field: str
    old_value: str | None
    new_value: str | None
    changed_by: str
    changed_at: datetime

class TenantLLMConfigAuditListResponse(BaseModel):
    entries: list[TenantLLMConfigAuditEntry]
    total: int
```

### 7.3 Write endpoints (existing, behavior changes)

| Endpoint | External change | Internal change |
|---|---|---|
| `POST /admin/tenants/{id}/llm-configs` | None (request/response identical) | Route computes `api_key_sha256`; wrapper writes 1-4 audit rows (always `api_key`; others if changed from old) |
| `PATCH /admin/tenants/{id}/llm-configs/{provider}` | None | Wrapper writes 0-N audit rows (only fields in `payload.model_fields_set` AND whose value actually changed) |
| `DELETE /admin/tenants/{id}/llm-configs/{provider}` | None | Wrapper writes 1 audit row (`__deleted__` marker); idempotent non-delete is unchanged (0 rows) |

## 8. Data Flow (concrete walks)

### 8.1 POST (insert)

```
T1 POST {provider:"anthropic", api_key:"sk-new", base_url:"https://proxy", model:"claude-haiku-4-5"}
  → route: api_key_sha256 = "abc123..."; changed_by = "01HZUSER..."
  → wrapper.upsert:
     old_row = None  (insert)
     inner.upsert(session, ...) → new_row
     diffs = [
       ("api_key",      "sha256:initial",    "sha256:abc123..."),
       ("base_url",     None,               "https://proxy"),
       ("model",        None,               "claude-haiku-4-5"),
       ("enabled",      None,               "true"),
     ]
     audit_repo.insert_many(session, 4 rows)
     commit
  → 201 + new_row; audit log has 4 rows
```

### 8.2 PATCH (partial)

```
T1 PATCH /anthropic {model: "claude-haiku-4-5", enabled: false}
  → route: changed_by = "01HZUSER..."
  → wrapper.update:
     old_row = {model: "claude-3-5-sonnet-20241022", enabled: True, base_url: "...", ...}
     payload.model_fields_set = {"model", "enabled"}
     inner.update(session, model="claude-haiku-4-5", enabled=False) → new_row
     diffs = [
       ("model",    "claude-3-5-sonnet-20241022", "claude-haiku-4-5"),  # changed
       ("enabled",  "true",                       "false"),             # changed
     ]
     # base_url not in fields_set → skipped (even if DB value differs)
     audit_repo.insert_many(session, 2 rows)
     commit
  → 200 + new_row; audit log has 2 rows
```

### 8.3 DELETE

```
T1 DELETE /anthropic
  → route: changed_by = "01HZUSER..."
  → wrapper.delete:
     old_row = {provider_name: "anthropic", ...}
     inner.delete_by_tenant_provider(session, T1, "anthropic")
     diffs = [("__deleted__", "anthropic", None)]
     audit_repo.insert_many(session, 1 row)
     commit
  → 204; audit log has 1 row (__deleted__ marker, api_key NOT audited)
```

### 8.4 Atomicity — audit write fails

```
T1 POST {provider:"anthropic", api_key:"sk-new", ...}
  → wrapper.upsert:
     old_row = None
     inner.upsert(session, ...) → new_row   # staged in session, not committed
     diffs = [...]
     audit_repo.insert_many(session, 4 rows)
     # ↑ monkey-patched to raise RuntimeError
  → exception propagates → SQLAlchemy auto-rollback → session closed
  → FastAPI returns 500
  → DB has neither a new tenant_llm_configs row NOR audit rows for this call
```

The same atomicity holds for PATCH and DELETE. M5.3 has dedicated regression tests for all three (see §10.3).

## 9. Error Handling

| Failure | Behavior | Reason |
|---|---|---|
| Audit INSERT fails | Whole transaction rolls back; mutation not applied; API returns 500 | Atomicity is the contract; half-success is worse than no audit |
| Mutation succeeds, response serialization fails | DB committed; client sees 500; client retries → upsert is idempotent, no duplicate state | Already-committed state is correct; serialization is transport-layer |
| Cross-tenant access on any endpoint | 404 (anti-enumeration, mirrors `list_tenant_llm_configs`) | Existing pattern |
| `?provider_name=bogus` (not in allowlist) | 422 | Literal validation; typo guard |
| `?provider_name=""` (empty) | 422 (`min_length=1`) | Disambiguate from "no filter" |
| `?limit=0` or `?limit=201` or `?offset=-1` | 422 | FastAPI Query bounds |
| DELETE of non-existent provider | 204 + 0 audit rows (idempotent unchanged) | Existing behavior preserved |
| Tenant row deleted (CASCADE) | All audit rows for that tenant CASCADE-delete | `ON DELETE CASCADE` on FK |
| Field value > 255 chars (e.g. 600-char `base_url`) | Stored in `Text` column; no truncation | Schema accepts any length |
| SHA-256 collision (effectively impossible) | Negligible; same as any hash collision | Acceptable |

## 10. Testing Strategy

Target: ~40 new tests + 1 E2E + 1 Explore round.

### 10.1 ORM + AuditRepository (~8 tests, `test_tenant_config_audit_models.py`)

- `test_insert_many_persists_rows`
- `test_list_by_tenant_orders_newest_first`
- `test_list_by_tenant_filters_by_provider`
- `test_list_by_tenant_pagination`
- `test_list_by_tenant_empty`
- `test_cascade_delete_with_tenant`
- `test_field_literal_rejects_unknown` — `TenantLLMConfigAuditLog(field="bogus")` raises
- `test_old_and_new_value_accept_text_length` — 600-char `base_url` round-trips

### 10.2 Diff helper unit tests (~9 tests, `test_admin_llm_config_diff.py`)

Pure-function tests, no DB:

- `test_post_insert_returns_four_rows` (old=None, full payload → 4)
- `test_post_update_changes_all_returns_four` (old+new, all different)
- `test_post_update_one_field_changed_returns_one`
- `test_post_identical_body_returns_zero`
- `test_patch_two_fields_changed_returns_two`
- `test_patch_no_actual_change_returns_zero`
- `test_patch_field_not_in_set_skipped`
- `test_patch_clears_to_null_returns_one`
- `test_delete_returns_deleted_marker`

### 10.3 Wrapper integration tests (~12 tests, `test_admin_llm_config_audit_integration.py`)

- `test_post_creates_audit_rows`
- `test_post_update_writes_old_new_hashes`
- `test_patch_one_field_audit_one_row`
- `test_patch_no_actual_change_no_audit`
- `test_delete_writes_deleted_marker`
- `test_delete_nonexistent_idempotent_no_audit`
- `test_changed_by_comes_from_claims`
- `test_api_key_sha256_matches_payload`
- **`test_audit_atomicity_post_rolls_back_on_audit_failure`** (monkey-patch `insert_many` → raise; assert 500 + DB unchanged)
- **`test_audit_atomicity_patch_rolls_back`** (same, PATCH)
- **`test_audit_atomicity_delete_rolls_back`** (same, DELETE)
- `test_inner_repo_back_compat_no_session_arg` (extend-seed.py path)

The three ⭐ atomicity tests are the most important: they prove the §4 "mutation + audit不可分割" contract.

### 10.4 GET endpoint tests (~10 tests, `test_tenant_llm_config_audit_api.py`)

- `test_get_audit_empty_tenant`
- `test_get_audit_returns_entries_newest_first`
- `test_get_audit_filter_by_provider`
- `test_get_audit_pagination`
- `test_get_audit_invalid_provider_returns_422`
- `test_get_audit_empty_provider_returns_422`
- `test_get_audit_without_token_returns_401`
- `test_get_audit_non_admin_token_returns_403`
- `test_get_audit_cross_tenant_returns_404`
- `test_get_audit_unknown_tenant_returns_404`

### 10.5 E2E script (`test_tenant_llm_config_audit_e2e.py`)

Mirrors `test_tenant_fallback_chain_e2e.py` shape (argparse + `[OK|WARN|FAIL|INFO]` + exit codes 0/1/2):

1. Mint tenant
2. POST `anthropic` config → assert 1 audit row
3. PATCH `model` → assert +1 audit row
4. PATCH `enabled=false` → assert +1 audit row
5. GET audit → assert 3 rows, DESC order
6. GET audit `?provider_name=anthropic&limit=2` → assert 2 rows
7. DELETE → assert +1 `__deleted__` row, total 4

### 10.6 Explore Round 11

Adds to `tests/e2e/scripts/explore.py` `ALL_ROUNDS`:

- Mint tenant + POST/PATCH/DELETE via real API
- `SELECT count(*) FROM tenant_llm_config_audit_log WHERE tenant_id = X` ≥ mutation count
- Sample 1 row, assert `changed_by` non-null and `field` in `{api_key, base_url, model, enabled, __deleted__}`
- Skip with `[WARN]` if no API creds in env (mirrors Round 10)

## 11. Migration

`apps/api/migrations/versions/23_tenant_llm_config_audit_log.py`:

```python
"""add tenant_llm_config_audit_log table (M5.3 audit log)

Revision ID: 23_tenant_llm_config_audit_log
Revises: 22_tenant_llm_configs_model
Create Date: 2026-10-12 19:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "23_tenant_llm_config_audit_log"
down_revision = "22_tenant_llm_configs_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_llm_config_audit_log",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(length=26),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("field", sa.String(length=32), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.String(length=64), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_audit_log_tenant_changed_at",
        "tenant_llm_config_audit_log",
        ["tenant_id", sa.text("changed_at DESC")],
    )
    op.create_index(
        "ix_audit_log_tenant_provider_changed_at",
        "tenant_llm_config_audit_log",
        ["tenant_id", "provider_name", sa.text("changed_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audit_log_tenant_provider_changed_at",
        table_name="tenant_llm_config_audit_log",
    )
    op.drop_index(
        "ix_audit_log_tenant_changed_at",
        table_name="tenant_llm_config_audit_log",
    )
    op.drop_table("tenant_llm_config_audit_log")
```

## 12. Observability

**One structlog INFO per mutation request** (not per audit row), written by the
wrapper after `session.commit()`:

```python
logger.info(
    "audit.llm_config.changed",
    extra={
        "tenant_id": tenant_id,
        "provider": provider_name,
        "action": "upsert" | "patch" | "delete",
        "fields_changed": [f for f, _, _ in diffs],   # e.g. ["api_key", "model"]
        "rows_written": len(diffs),
        "changed_by": changed_by,
    },
)
```

Rationale: POST with 4 audit rows logs ONE line with `fields_changed=["api_key",
"base_url", "model", "enabled"]` and `rows_written=4`. Grep-friendly without
N-times amplification. The hash prefixes I sketched in earlier drafts were
too noisy and would leak visibility into every byte of every key change; the
field list is sufficient for ops correlation.

The GET audit endpoint logs only on 4xx/5xx. No INFO logs on successful reads
(would flood).

## 13. Out of Scope (deferred)

- **Restore-to-T endpoint** — operator runs manual SQL if needed.
- **Retention cleanup worker** — operators run `DELETE ... WHERE changed_at < ...` manually.
- **Audit for non-admin sources** — seed script, future bulk paths.
- **CSV/JSON export** — JSON via GET only.
- **`field` query filter on GET** — client-side filter.
- **New Prometheus counter** — `lumen_llm_config_audit_writes_total` deferred.
- **Per-tenant rate limit on GET** — same as `list_tenant_tickets` today.
- **Replay-friendly timestamp snapshots** — pure event log; reconstruction requires manual SQL.
- **Audit log encryption at rest** — audit rows are operational metadata, not secrets (the SHA-256 fingerprint is non-reversible without the plaintext).

## 14. Verification

```bash
# 1. Migration
cd apps/api && uv run alembic upgrade head

# 2. Full M5.3 test suite
cd apps/api && uv run pytest \
    tests/llm_client/test_tenant_config_audit_models.py \
    tests/admin/test_admin_llm_config_diff.py \
    tests/admin/test_admin_llm_config_audit_integration.py \
    tests/admin/test_tenant_llm_config_audit_api.py \
    -v -m integration

# 3. Regression: existing llm-configs + M5.2 + M5.1 still pass
cd apps/api && uv run pytest \
    tests/admin/test_tenant_llm_config_api.py \
    tests/llm_client/test_tenant_config_models_model.py \
    tests/llm_client/test_tenant_model_override_resolver.py \
    tests/llm_client/test_tenant_resolver_with_model_override.py \
    tests/llm_client/test_tenant_fallback_chain_models.py \
    tests/llm_client/test_tenant_fallback_chain_cache.py \
    tests/llm_client/test_tenant_resolver_with_chain.py \
    -v -m integration

# 4. E2E
python tests/e2e/scripts/test_tenant_llm_config_audit_e2e.py --api-base http://localhost:8000

# 5. Ruff
cd apps/api && uv run ruff check \
    src/llm_client/tenant_config_audit_models.py \
    src/llm_client/tenant_config_models.py \
    src/admin/repository.py \
    src/admin/api.py \
    src/admin/schemas/tenant_config_audit.py

# 6. Smoke: hit the live API
curl -X POST http://localhost:8000/api/v1/admin/tenants/$TID/llm-configs \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -H "Content-Type: application/json" \
  -d '{"provider_name":"anthropic","api_key":"sk-test","model":"claude-haiku-4-5"}'
curl http://localhost:8000/api/v1/admin/tenants/$TID/llm-configs/audit \
  -H "Authorization: Bearer $ADMIN_JWT"
```

**Pass criteria:**
- All 40+ new tests pass
- 3 atomicity tests pass (proves the §4 contract)
- `extend-seed.py` still works (inner repo back-compat)
- E2E all 7 steps pass against live API
- Ruff clean on all 5 modified files
- README row 30 added; tech debt line 372 marked DONE
