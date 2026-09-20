# Tech Debt #17 — Admin KB Drafts JWT Auth

> **Status:** design approved (M3 kickoff). Implementation plan: `docs/superpowers/plans/2026-09-20-tech-debt-17-admin-jwt-auth.md` (TBD).

## Goal

Remove spoofable `tenant_id` / `reviewer_id` `Query()` parameters from the 4 admin KB-draft endpoints; replace with `Depends(require_admin)` so both fields are derived from the verified JWT claims.

## Context

`apps/api/src/admin/api.py` (Stage 18 / M2.B Task 8) exposes 4 admin endpoints that currently accept `tenant_id: str = Query(...)` and (where applicable) `reviewer_id: str = Query(...)`. These are NOT verified by the server — an attacker who knows or guesses any ULID can probe cross-tenant drafts and either submit spurious approve/reject actions on real tenants' drafts or pull body content via the GET endpoint.

The `auth.dependencies:require_admin` dependency already exists (`apps/api/src/auth/dependencies.py:44-61`); it raises `401` on missing/invalid token and `403` when the token's role is neither `admin` nor `owner`. The canonical JWT-protect pattern is established in `apps/api/src/agent/api.py` (lines 80–83, 115–117, 177–183) and `apps/api/src/knowledge/api.py` (lines 213–214, 226–227). This spec is wiring the admin endpoints to that same dependency.

## Approach

**Replace `Query()` with `Depends(require_admin)`.** Two parameters disappear from every signature; `tenant_id` and `reviewer_id` are read from the returned claims dict (`claims["tenant_id"]`, `claims["sub"]`).

### File-level changes

#### `apps/api/src/admin/api.py`

1. Replace import line 45: drop `Query` from `fastapi`; add `Annotated`, `Any` from `typing`; add `Depends` to `fastapi`; add `require_admin` from `auth.dependencies`.
2. Replace the `KNOWN DEBT` block at lines 20–27 with a brief note that auth now flows through JWT and points at this spec.
3. Replace the `# TODO tech-debt #17` comment at line 63 with: `# tenant + reviewer identity derived from JWT claims via require_admin`.
4. **`GET /kb-drafts`** (lines 142–180): replace signature
   ```python
   async def list_kb_drafts(
       tenant_id: str = Query(...),
       status: str = Query("DRAFT"),
       limit: int = Query(50, le=200),
   ):
   ```
   with
   ```python
   async def list_kb_drafts(
       claims: Annotated[dict[str, Any], Depends(require_admin)],
       status: str = Query("DRAFT"),
       limit: int = Query(50, le=200),
   ):
       tenant_id = claims["tenant_id"]
   ```
5. **`GET /kb-drafts/{draft_id}`** (lines 183–215): same pattern, replace `tenant_id: str = Query(...)` with the dep.
6. **`POST /kb-drafts/{draft_id}/approve`** (lines 218–314): replace `tenant_id` and `reviewer_id` Query params; inside the body, derive them from claims.
7. **`POST /kb-drafts/{draft_id}/reject`** (lines 317–362): same as approve.
8. The `extra={"reviewer_id": reviewer_id}` payloads in the two `logger.info` calls (lines 305, 357) keep working because `reviewer_id` is now a local variable derived from claims, not a Query param.

### Test changes

#### `apps/api/tests/admin/conftest.py`

Add `sample_admin_token` fixture that issues a JWT signed by the test app secret with claims `{"sub": "admin-1", "tenant_id": <tenant.id>, "role": "admin"}`. Helper function `make_admin_headers(token) -> dict[str, str]` returns `{"Authorization": f"Bearer {token}"}`.

Add `sample_non_admin_token` fixture for the role-check test (e.g. role `agent`).

#### `apps/api/tests/admin/test_kb_drafts_api.py`

Every existing call that passes `params={"tenant_id": ..., "reviewer_id": ...}` (lines 50–53, 188–191, 238–241, 290–293, 325–328, 357–359, 386–388) is updated to pass `headers=make_admin_headers(admin_token)` instead. The `tenant_id` inside the body (used for cross-tenant assertions) is read from the test's `sample_tenant.id` fixture, NOT from the request. The `reviewer_id` in the audit-row assertions (if any) reads from the token's `sub` claim.

#### New test cases in the same file

- `test_list_drafts_without_token_returns_401` — no Authorization header → 401
- `test_list_drafts_with_non_admin_token_returns_403` — role `agent` → 403
- `test_get_draft_without_token_returns_401`
- `test_approve_without_token_returns_401`
- `test_approve_with_non_admin_token_returns_403`
- `test_reject_without_token_returns_401`
- `test_approve_uses_jwt_sub_as_reviewer` — after approve, `draft.reviewed_by` equals the token's `sub` claim
- `test_approve_uses_jwt_tenant_id` — passing a token whose `tenant_id` differs from the draft's `tenant_id` returns 404 (cross-tenant isolation preserved)

## Anti-enumeration preservation

Every `with_for_update()` SELECT already carries `KbArticleDraft.tenant_id == tenant_id` (lines 246–247, 336–337) — the WHERE clause now reads from JWT claims instead of a Query string, but the safety property is identical. The "not found vs. wrong tenant" 404 collapse stays.

## PII discipline

No new log lines; existing `extra={...}` payloads already use opaque IDs only (`tenant_id`, `draft_id`, `article_id`, `reviewer_id`). No change needed.

## Out of scope

- Role definitions, JWT signing key, token expiry — all owned by `auth.service` and out of scope here.
- Audit log of admin actions — separate spec; not required to close this debt.
- Per-admin role granularity (e.g. "drafts:read" vs "drafts:approve") — single `admin` role covers all 4 endpoints for now.

## Acceptance

- [ ] All 4 admin endpoints require JWT; missing token → 401; non-admin role → 403.
- [ ] Tenant identity is read from JWT claims, never from URL / header / query.
- [ ] Reviewer identity equals `claims["sub"]`; cross-tenant access still 404.
- [ ] All existing `test_kb_drafts_api.py` tests still pass after switching to `headers=` from `params=`.
- [ ] No new log lines emit tenant PII; reviewer_id is the opaque ULID.
