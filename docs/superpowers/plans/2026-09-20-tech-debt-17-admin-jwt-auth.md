# Tech Debt #17 — Admin JWT Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace spoofable `Query()` `tenant_id` and `reviewer_id` parameters on the 4 admin KB-draft endpoints with `Depends(require_admin)`; derive both from JWT claims.

**Architecture:** Single-file refactor of `apps/api/src/admin/api.py` plus targeted test fixture updates. `auth.dependencies.require_admin` already exists (`apps/api/src/auth/dependencies.py:44–61`) and is the canonical role gate; we adopt it verbatim. The JWT helper `auth.jwt.create_access_token` is used to issue admin tokens in tests.

**Tech Stack:** FastAPI, Pydantic v2, pytest, httpx AsyncClient, jose (JWT).

---

## File Structure

| Path | Role |
|------|------|
| `apps/api/src/admin/api.py` | 4 admin endpoints — replace Query() with Depends(require_admin) |
| `apps/api/tests/admin/conftest.py` | Add `admin_token_for` + `non_admin_token_for` fixtures + `auth_headers()` helper |
| `apps/api/tests/admin/test_kb_drafts_api.py` | Update all `params=` → `headers=`; add 7 new auth test cases |

No new files in `src/`. No new dependencies.

---

## Task 1: Add JWT helper fixtures to admin conftest

**Files:**
- Modify: `apps/api/tests/admin/conftest.py` (append before `__all__`)

- [ ] **Step 1: Add the helper functions and fixtures**

Append the following to `apps/api/tests/admin/conftest.py` (insert before the `__all__` line at the bottom):

```python
from auth.jwt import create_access_token


def auth_headers(token: str) -> dict[str, str]:
    """Build the ``Authorization`` header dict for a JWT bearer token."""
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_token_for():
    """Return a callable that issues an admin JWT for the given tenant.

    Usage in a test::

        async def test_x(async_client, sample_tenant, admin_token_for):
            token = admin_token_for(tenant_id=sample_tenant.id, user_id="admin-1")
            resp = await async_client.get("/api/v1/admin/kb-drafts",
                                           headers=auth_headers(token))
    """

    def _make(*, tenant_id: str, user_id: str = "admin-1") -> str:
        return create_access_token(
            tenant_id=tenant_id, user_id=user_id, role="admin"
        )

    return _make


@pytest.fixture
def non_admin_token_for():
    """Return a callable that issues a non-admin (agent) JWT for the given tenant."""

    def _make(*, tenant_id: str, user_id: str = "agent-1") -> str:
        return create_access_token(
            tenant_id=tenant_id, user_id=user_id, role="agent"
        )

    return _make


__all__ = [
    "admin_token_for",
    "async_client",
    "auth_headers",
    "db_session",
    "non_admin_token_for",
    "sample_tenant",
]
```

(Replace the existing `__all__ = [...]` at the bottom with the new list.)

- [ ] **Step 2: Run conftest collection check**

Run:
```bash
cd apps/api && pytest tests/admin/conftest.py --collect-only -q
```
Expected: collection succeeds (no tests in conftest itself but the file imports cleanly).

- [ ] **Step 3: Commit**

```bash
git add apps/api/tests/admin/conftest.py
git commit -m "test(admin): add admin_token_for + non_admin_token_for fixtures"
```

---

## Task 2: Add failing auth tests for the 4 endpoints

**Files:**
- Modify: `apps/api/tests/admin/test_kb_drafts_api.py` (append new test cases)

- [ ] **Step 1: Add the 7 new auth test cases at the end of the file**

```python
# ---------------------------------------------------------------------------
# Tech debt #17 — JWT auth on all 4 endpoints
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_drafts_without_token_returns_401(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    """No Authorization header → 401 (auth gate fires before tenant filter)."""
    resp = await async_client.get(
        "/api/v1/admin/kb-drafts",
        params={"tenant_id": sample_tenant.id},
    )
    assert resp.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_drafts_with_non_admin_token_returns_403(
    async_client: AsyncClient,
    sample_tenant: Tenant,
    non_admin_token_for,
) -> None:
    """Agent role → 403 (require_admin blocks)."""
    token = non_admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        "/api/v1/admin/kb-drafts",
        headers=auth_headers(token),
    )
    assert resp.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_draft_without_token_returns_401(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    resp = await async_client.get(
        f"/api/v1/admin/kb-drafts/{new_id()}",
        params={"tenant_id": sample_tenant.id},
    )
    assert resp.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approve_without_token_returns_401(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{new_id()}/approve",
        params={"tenant_id": sample_tenant.id, "reviewer_id": "admin1"},
    )
    assert resp.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approve_with_non_admin_token_returns_403(
    async_client: AsyncClient,
    sample_tenant: Tenant,
    non_admin_token_for,
) -> None:
    token = non_admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{new_id()}/approve",
        headers=auth_headers(token),
    )
    assert resp.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_without_token_returns_401(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{new_id()}/reject",
        params={"tenant_id": sample_tenant.id, "reviewer_id": "admin1"},
    )
    assert resp.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approve_uses_jwt_sub_as_reviewer(
    async_client: AsyncClient,
    sample_tenant: Tenant,
    admin_token_for,
) -> None:
    """After approve, draft.reviewed_by equals the JWT's sub claim, not a Query param."""
    sm = get_sessionmaker()
    draft_id = new_id()
    async with sm() as session:
        session.add(
            KbArticleDraft(
                id=draft_id,
                tenant_id=sample_tenant.id,
                cluster_id=0,
                source_questions=["q1"],
                suggested_title="T",
                suggested_body="b",
                suggested_tags=[],
                status="DRAFT",
            )
        )
        await session.commit()

    token = admin_token_for(tenant_id=sample_tenant.id, user_id="custom-admin-id")
    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{draft_id}/approve",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text

    async with sm() as session:
        draft_row = (
            await session.execute(
                select(KbArticleDraft).where(KbArticleDraft.id == draft_id)
            )
        ).scalar_one()
        assert draft_row.reviewed_by == "custom-admin-id"
```

- [ ] **Step 2: Run the new tests — verify they fail (401/403 expected but currently no auth gate = endpoint accessible)**

Run:
```bash
cd apps/api && pytest tests/admin/test_kb_drafts_api.py -k "without_token or non_admin_token or jwt_sub" -v
```
Expected: 7 tests fail. The first 6 fail because the current implementation has NO auth gate (returns 200). The 7th (`test_approve_uses_jwt_sub_as_reviewer`) fails because the current code reads `reviewer_id` from Query, not JWT — it'll either 422 (missing Query param) or store "admin1" if we provide it.

Specifically:
- `test_list_drafts_without_token_returns_401` — currently returns 200 (no gate). Expected FAIL.
- `test_list_drafts_with_non_admin_token_returns_403` — currently returns 200. Expected FAIL.
- `test_get_draft_without_token_returns_401` — currently returns 404 (no draft with that ID). Expected FAIL.
- `test_approve_without_token_returns_401` — currently returns 404. Expected FAIL.
- `test_approve_with_non_admin_token_returns_403` — currently returns 404. Expected FAIL.
- `test_reject_without_token_returns_401` — currently returns 404. Expected FAIL.
- `test_approve_uses_jwt_sub_as_reviewer` — currently passes `reviewer_id` via Query (`admin1`), so `draft.reviewed_by == "custom-admin-id"` fails. Expected FAIL.

- [ ] **Step 3: Commit the failing tests**

```bash
git add apps/api/tests/admin/test_kb_drafts_api.py
git commit -m "test(admin): failing JWT-auth tests for 4 admin endpoints"
```

---

## Task 3: Refactor `admin/api.py` — add imports

**Files:**
- Modify: `apps/api/src/admin/api.py:40-65` (imports + module docstring)

- [ ] **Step 1: Update the import block and KNOWN DEBT comment**

In `apps/api/src/admin/api.py`:

1. **Lines 45** — replace the existing `from fastapi import APIRouter, HTTPException, Query` with:
   ```python
   from typing import Annotated, Any

   from fastapi import APIRouter, Depends, HTTPException, Query

   from auth.dependencies import require_admin
   ```

2. **Lines 20–27** — replace the `KNOWN DEBT` block with:
   ```python
   Tenant + reviewer identity
   ----------------------

   All 4 endpoints derive both fields from the JWT claims via
   ``Depends(require_admin)``. ``claims["tenant_id"]`` is the
   authoritative tenant for WHERE clauses; ``claims["sub"]`` is the
   reviewer recorded on the audit row. Cross-tenant access still
   returns 404 (anti-enumeration parity with M1).
   ```

3. **Line 63** — delete the `# TODO tech-debt #17: replace Query() ...` comment entirely.

- [ ] **Step 2: Verify import compiles**

Run:
```bash
cd apps/api && python -c "from admin.api import router; print('imports ok')"
```
Expected: `imports ok`.

- [ ] **Step 3: Commit**

```bash
git add apps/api/src/admin/api.py
git commit -m "refactor(admin): switch to Depends(require_admin) — imports"
```

---

## Task 4: Refactor `GET /kb-drafts` (list endpoint)

**Files:**
- Modify: `apps/api/src/admin/api.py:142-180` (the `list_kb_drafts` function)

- [ ] **Step 1: Replace the function signature and derive `tenant_id`**

Replace the `list_kb_drafts` function (lines 142–180) with:

```python
@router.get("/kb-drafts")
async def list_kb_drafts(
    claims: Annotated[dict[str, Any], Depends(require_admin)],
    status: str = Query("DRAFT"),
    limit: int = Query(50, le=200),
):
    """List KB drafts for review.

    Tenant is derived from the verified JWT (require_admin). Status
    filter defaults to ``DRAFT`` (the only reviewable state). Cross-
    tenant access is impossible because the WHERE clause carries the
    authenticated tenant — there is no per-row 404 on the LIST
    endpoint (the security boundary is per-row, not per-call).
    """
    tenant_id = claims["tenant_id"]
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(
            select(KbArticleDraft)
            .where(
                KbArticleDraft.tenant_id == tenant_id,
                KbArticleDraft.status == status,
            )
            .order_by(KbArticleDraft.created_at.desc())
            .limit(limit)
        )
        drafts = result.scalars().all()
        return {
            "drafts": [
                {
                    "id": d.id,
                    "title": d.suggested_title,
                    "body_preview": d.suggested_body[:_BODY_PREVIEW_LEN],
                    "tags": d.suggested_tags or [],
                    "source_question_count": len(d.source_questions or []),
                    "cluster_id": d.cluster_id,
                    "status": d.status,
                    "created_at": d.created_at.isoformat(),
                }
                for d in drafts
            ]
        }
```

- [ ] **Step 2: Run the new 401/403 tests for this endpoint**

Run:
```bash
cd apps/api && pytest tests/admin/test_kb_drafts_api.py -k "list_drafts_without_token or list_drafts_with_non_admin" -v
```
Expected: both tests pass (401 + 403 respectively).

- [ ] **Step 3: Run the existing list test (it currently uses `params=`) — expect failure**

Run:
```bash
cd apps/api && pytest tests/admin/test_kb_drafts_api.py::test_list_drafts_returns_only_tenant_drafts tests/admin/test_kb_drafts_api.py::test_list_drafts_filters_by_status tests/admin/test_kb_drafts_api.py::test_list_for_foreign_tenant_returns_empty -v
```
Expected: 3 tests fail because they still pass `params={"tenant_id": ...}` (no auth header). Fix in Task 7.

- [ ] **Step 4: Commit**

```bash
git add apps/api/src/admin/api.py
git commit -m "refactor(admin): JWT-auth the GET /kb-drafts list endpoint"
```

---

## Task 5: Refactor `GET /kb-drafts/{draft_id}` (detail endpoint)

**Files:**
- Modify: `apps/api/src/admin/api.py:183-215`

- [ ] **Step 1: Replace the function signature**

Replace the `get_kb_draft` function (lines 183–215) with:

```python
@router.get("/kb-drafts/{draft_id}")
async def get_kb_draft(
    draft_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
):
    """Return full draft details (incl. full ``suggested_body``).

    Without this endpoint, admins can only see the first 200 chars of
    the suggested body — they literally cannot decide approve/reject
    end-to-end. Cross-tenant access returns 404 (anti-enumeration,
    matching the other per-row endpoints).
    """
    tenant_id = claims["tenant_id"]
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(
            select(KbArticleDraft).where(
                KbArticleDraft.id == draft_id,
                KbArticleDraft.tenant_id == tenant_id,
            )
        )
        draft = result.scalar_one_or_none()
        if draft is None:
            raise HTTPException(status_code=404, detail="draft not found")
        return {
            "id": draft.id,
            "title": draft.suggested_title,
            "body": draft.suggested_body,
            "tags": draft.suggested_tags or [],
            "status": draft.status,
            "created_at": draft.created_at.isoformat(),
            "reviewed_at": draft.reviewed_at.isoformat() if draft.reviewed_at else None,
            "reviewed_by": draft.reviewed_by,
        }
```

- [ ] **Step 2: Run the new 401 test**

Run:
```bash
cd apps/api && pytest tests/admin/test_kb_drafts_api.py::test_get_draft_without_token_returns_401 -v
```
Expected: PASS (returns 401).

- [ ] **Step 3: Commit**

```bash
git add apps/api/src/admin/api.py
git commit -m "refactor(admin): JWT-auth the GET /kb-drafts/{draft_id} detail endpoint"
```

---

## Task 6: Refactor `POST /kb-drafts/{draft_id}/approve` and `/reject`

**Files:**
- Modify: `apps/api/src/admin/api.py:218-314` (approve) and `317-362` (reject)

- [ ] **Step 1: Replace the approve function**

Replace the `approve_kb_draft` function (lines 218–314) with:

```python
@router.post("/kb-drafts/{draft_id}/approve")
async def approve_kb_draft(
    draft_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
):
    """Approve → create Article + mark draft APPROVED.

    Pessimistic lock on KbArticleDraft row prevents two admins from
    double-approving the same draft (race producing 2 Article rows + lost audit).

    Lifecycle::

        DRAFT  →  APPROVED  + Article created in auto-mined KB

    Status codes
    ----------
    * 200 — approved (returns ``{draft_id, article_id, status}``)
    * 401 — missing / invalid bearer token (raised by require_admin)
    * 403 — token is not admin / owner role (raised by require_admin)
    * 404 — draft not found OR belongs to a different tenant (no
      distinction — anti-enumeration)
    * 409 — draft is already APPROVED or REJECTED
    """
    tenant_id = claims["tenant_id"]
    reviewer_id = claims["sub"]
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(
            select(KbArticleDraft)
            .where(
                KbArticleDraft.id == draft_id,
                KbArticleDraft.tenant_id == tenant_id,
            )
            .with_for_update()  # Pessimistic lock for the duration of this transaction
        )
        draft = result.scalar_one_or_none()
        if draft is None:
            raise HTTPException(status_code=404, detail="draft not found")
        if draft.status != "DRAFT":
            raise HTTPException(
                status_code=409, detail=f"draft already {draft.status}"
            )

        # Ensure the tenant has an auto-mined KB to land the article in.
        kb = await _ensure_auto_mined_kb(session, tenant_id=tenant_id)

        # Deterministic content hash — the draft body becomes the
        # ArticleVersion.raw_text, and the hash drives dedup on future
        # reindex attempts against the same bytes.
        content_hash = hashlib.sha256(
            draft.suggested_body.encode("utf-8")
        ).hexdigest()

        article_id = new_id()
        article = Article(
            id=article_id,
            tenant_id=tenant_id,
            knowledge_base_id=kb.id,
            title=draft.suggested_title,
            source_type=ArticleSourceType.MANUAL,
            status=ArticleStatus.INDEXING,
        )
        session.add(article)

        version = ArticleVersion(
            id=new_id(),
            article_id=article_id,
            version_number=1,
            raw_text=draft.suggested_body,
            content_hash=content_hash,
        )
        session.add(version)

        # Back-link: Article.current_version_id points at the version row.
        # We flush so the version row is visible to the UPDATE below.
        await session.flush()
        article.current_version_id = version.id

        # Promote the draft.
        draft.status = "APPROVED"
        draft.published_article_id = article_id
        draft.reviewed_at = datetime.now(timezone.utc)
        draft.reviewed_by = reviewer_id

        await session.commit()

    logger.info(
        "admin.kb_draft.approved",
        extra={
            "tenant_id": tenant_id,
            "draft_id": draft_id,
            "article_id": article_id,
            "reviewer_id": reviewer_id,
        },
    )
    return {
        "draft_id": draft_id,
        "article_id": article_id,
        "status": "APPROVED",
    }
```

- [ ] **Step 2: Replace the reject function**

Replace the `reject_kb_draft` function (lines 317–362) with:

```python
@router.post("/kb-drafts/{draft_id}/reject")
async def reject_kb_draft(
    draft_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
):
    """Reject a DRAFT → mark ``REJECTED``. No ``Article`` is created.

    Status codes
    ----------
    * 200 — rejected
    * 401 / 403 — auth gate (raised by require_admin)
    * 404 — not found / cross-tenant
    * 409 — already APPROVED / REJECTED
    """
    tenant_id = claims["tenant_id"]
    reviewer_id = claims["sub"]
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(
            select(KbArticleDraft)
            .where(
                KbArticleDraft.id == draft_id,
                KbArticleDraft.tenant_id == tenant_id,
            )
            .with_for_update()  # Pessimistic lock — same rationale as approve
        )
        draft = result.scalar_one_or_none()
        if draft is None:
            raise HTTPException(status_code=404, detail="draft not found")
        if draft.status != "DRAFT":
            raise HTTPException(
                status_code=409, detail=f"draft already {draft.status}"
            )

        draft.status = "REJECTED"
        draft.reviewed_at = datetime.now(timezone.utc)
        draft.reviewed_by = reviewer_id
        await session.commit()

    logger.info(
        "admin.kb_draft.rejected",
        extra={
            "tenant_id": tenant_id,
            "draft_id": draft_id,
            "reviewer_id": reviewer_id,
        },
    )
    return {"draft_id": draft_id, "status": "REJECTED"}
```

- [ ] **Step 3: Run all new auth tests**

Run:
```bash
cd apps/api && pytest tests/admin/test_kb_drafts_api.py -k "without_token or non_admin_token or jwt_sub" -v
```
Expected: all 7 new tests PASS.

- [ ] **Step 4: Commit**

```bash
git add apps/api/src/admin/api.py
git commit -m "refactor(admin): JWT-auth approve + reject endpoints"
```

---

## Task 7: Update existing tests to use JWT headers

**Files:**
- Modify: `apps/api/tests/admin/test_kb_drafts_api.py` (8 test functions)

- [ ] **Step 1: Update `test_list_drafts_returns_only_tenant_drafts` (lines 29–60)**

Change the request line (around line 50–53) from:
```python
    resp = await async_client.get(
        "/api/v1/admin/kb-drafts",
        params={"tenant_id": sample_tenant.id},
    )
```
to:
```python
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        "/api/v1/admin/kb-drafts",
        headers=auth_headers(token),
    )
```

Also update the signature: add `admin_token_for` to the parameters:
```python
async def test_list_drafts_returns_only_tenant_drafts(
    async_client: AsyncClient,
    sample_tenant: Tenant,
    admin_token_for,
) -> None:
```

- [ ] **Step 2: Update `test_list_drafts_filters_by_status` (lines 65–92)**

Same pattern as Step 1. Add `admin_token_for` parameter, swap `params=` for `headers=auth_headers(admin_token_for(tenant_id=sample_tenant.id))`.

- [ ] **Step 3: Update `test_get_draft_returns_full_body` (lines 97–131)**

Same pattern. Add `admin_token_for`, swap params → headers.

- [ ] **Step 4: Update `test_get_draft_cross_tenant_returns_404` (lines 136–162)**

Same pattern. The test seeds a draft in `other_tenant_id` and queries as `sample_tenant` — the JWT is for `sample_tenant`, so the WHERE filter excludes the foreign draft and we get 404.

- [ ] **Step 5: Update `test_approve_draft_creates_article_and_marks_approved` (lines 167–212)**

Same pattern. The assertion `assert draft_row.reviewed_by == "admin1"` (line 211) must change to assert against the token's `sub`. After this step, it should be `assert draft_row.reviewed_by == "admin-1"` (the default user_id from the `admin_token_for` fixture).

- [ ] **Step 6: Update `test_reject_draft_does_not_create_article` (lines 217–262)**

Same pattern.

- [ ] **Step 7: Update `test_cross_tenant_approve_returns_404` (lines 267–294)**

Same pattern. JWT for `sample_tenant`, draft belongs to `other_tenant_id` → 404.

- [ ] **Step 8: Update `test_list_for_foreign_tenant_returns_empty` (lines 299–330)**

Same pattern.

- [ ] **Step 9: Update `test_approve_already_approved_returns_409` (lines 335–360)**

Same pattern.

- [ ] **Step 10: Update `test_reject_already_rejected_returns_409` (lines 365–390)**

Same pattern.

- [ ] **Step 11: Run the entire admin test file**

Run:
```bash
cd apps/api && pytest tests/admin/ -v -m integration
```
Expected: ALL tests pass (17 total: 10 existing + 7 new). No skips.

- [ ] **Step 12: Commit**

```bash
git add apps/api/tests/admin/test_kb_drafts_api.py
git commit -m "test(admin): switch all existing admin tests from params to JWT headers"
```

---

## Task 8: Final verification + remove from README tech debt list

**Files:**
- Modify: `README.md` (remove tech debt #17 entry)

- [ ] **Step 1: Run the entire non-integration suite as a smoke test**

Run:
```bash
cd apps/api && pytest --collect-only -m "not integration" 2>&1 | tail -5
```
Expected: collection succeeds, 0 errors.

- [ ] **Step 2: Run a wider integration sweep**

Run:
```bash
cd apps/api && pytest tests/admin/ tests/auth/ -m integration -v
```
Expected: all pass.

- [ ] **Step 3: Remove tech-debt #17 from the README**

Find the tech-debt #17 entry in `README.md` (something like "Admin KB drafts API uses `Query()` for tenant_id — auth gap") and either strike it through or delete the bullet. Confirm the line is no longer in the file:

```bash
grep -n "tech-debt #17\|admin KB drafts API" README.md
```
Expected: no match (or only struck-through).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: remove tech-debt #17 entry (admin JWT auth shipped)"
```

---

## Acceptance Checklist

- [ ] All 4 admin endpoints require a valid bearer token (401 otherwise) AND require admin/owner role (403 otherwise).
- [ ] `tenant_id` and `reviewer_id` are NEVER read from `Query()` / headers / body on the admin endpoints — only from JWT claims.
- [ ] Cross-tenant access still returns 404 (anti-enumeration parity).
- [ ] All 17 admin tests pass (10 existing + 7 new auth tests).
- [ ] `README.md` no longer lists #17 as outstanding tech debt.
