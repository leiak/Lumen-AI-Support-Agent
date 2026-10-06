"""Admin API for KB draft review (approve / reject).

Stage 18 / M2.B Task 8.

Endpoints
---------

* ``GET    /api/v1/admin/kb-drafts`` — list drafts for review, status filter
* ``POST    /api/v1/admin/kb-drafts/{id}/approve`` — promote to live KB article
* ``POST    /api/v1/admin/kb-drafts/{id}/reject`` — mark REJECTED, no article

Multi-tenant isolation
----------------------

Every query carries ``tenant_id`` — cross-tenant access returns
``None`` (404, NOT 403). Anti-enumeration: an attacker probing
draft IDs across tenants cannot distinguish "exists but yours"
from "doesn't exist".

Tech debt #17 — JWT auth on all 6 admin endpoints (M4.C Task 4 review):
``tenant_id`` is derived from ``claims["tenant_id"]`` (via
``Depends(require_admin)``); ``reviewer_id`` from ``claims["sub"]``.
Cross-tenant access returns 404 (anti-enumeration parity with M1).

PII discipline
--------------

The listing endpoint returns ``body_preview`` (first 200 chars of the
suggested body) and the title — those are LLM-generated text on top
of customer questions, not raw customer text. ``source_questions``
(message ULIDs) is NEVER returned in the listing body; the admin UI
should resolve those IDs against the messages table on the
server side if it needs to display the underlying questions.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from auth.dependencies import require_admin
from core.database import get_sessionmaker
from core.id_gen import new_id
from knowledge.enums import ArticleSourceType, ArticleStatus
from knowledge.models import (
    Article,
    ArticleVersion,
    KbArticleDraft,
    KnowledgeBase,
)

from admin.repository import (
    AdminTenantBudgetRepository,
    AdminTenantLLMConfigRepository,
)
from admin.schemas.budget import (
    BreakdownItem,
    CleanupResponse,
    CreditListResponse,
    CreditRequest,
    CreditResponse,
    TenantBudgetCreate,
    TenantBudgetRead,
    TenantBudgetUsageRead,
)
from admin.schemas.tenant_llm_config import (
    TenantLLMConfigCreate,
    TenantLLMConfigRead,
)
from budget.cleanup import run_budget_cleanup
from budget.credits import CreditService
from budget.per_model import PerModelService, get_per_model_cache
from budget.repository import TenantBudgetSnapshotRepository
from budget.resolver import _current_period
from tenant.repository import TenantRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Slug for the "system-mined" KB that promoted drafts land in. Auto-created
# per tenant on first approve so the admin doesn't have to wire a KB
# up-front. Real production would let admins choose a target KB at approve
# time — that's a UI story, not an API story.
_AUTO_MINED_KB_SLUG = "auto-mined-kb"
_AUTO_MINED_KB_NAME = "Auto-mined Knowledge"
# Bytes cap for the initial article body when a draft is approved. Matches
# ``knowledge.parser.MAX_PARSE_BYTES`` so the resulting article is
# parseable by the same ingest path.
_BODY_PREVIEW_LEN = 200


# ---------------------------------------------------------------------------
# KB provisioning
# ---------------------------------------------------------------------------


async def _ensure_auto_mined_kb(session, *, tenant_id: str) -> KnowledgeBase:
    """Return the tenant's auto-mined KB, or create it if missing.

    Idempotent: catches ``IntegrityError`` on the unique slug index so a
    race between two admins approving the first draft doesn't blow up.
    """
    existing = (
        await session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.slug == _AUTO_MINED_KB_SLUG,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    kb = KnowledgeBase(
        id=new_id(),
        tenant_id=tenant_id,
        slug=_AUTO_MINED_KB_SLUG,
        name=_AUTO_MINED_KB_NAME,
        description=(
            "Knowledge base populated by the history-mining worker. "
            "Each Article here originated as a KbArticleDraft approved "
            "via /admin/kb-drafts/{id}/approve."
        ),
    )
    session.add(kb)
    try:
        await session.flush()
    except IntegrityError:  # pragma: no cover — race between two approves
        # A concurrent admin approved first → refetch the now-existing row.
        await session.rollback()
        existing = (
            await session.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.tenant_id == tenant_id,
                    KnowledgeBase.slug == _AUTO_MINED_KB_SLUG,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing
    return kb


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# M4.C Task 4 — Tenant LLM config (BYOK) admin endpoints.
#
# POST /api/v1/admin/tenants/{tenant_id}/llm-configs
#   upsert (encrypts the plaintext API key with Fernet before storage;
#   response NEVER includes the decrypted key or the ciphertext)
#
# GET /api/v1/admin/tenants/{tenant_id}/llm-configs
#   list providers (provider_name + base_url + enabled + timestamps);
#   response NEVER includes key fields
#
# Tenant-existence check lives in AdminTenantLLMConfigRepository —
# 404 is returned for unknown tenant_ids, matching the anti-enumeration
# pattern used by the kb-drafts endpoints above.
# ---------------------------------------------------------------------------


@router.post(
    "/tenants/{tenant_id}/llm-configs",
    response_model=TenantLLMConfigRead,
    status_code=201,
)
async def create_or_update_tenant_llm_config(
    tenant_id: str,
    payload: TenantLLMConfigCreate,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> TenantLLMConfigRead:
    """Upsert a tenant's LLM provider config (M4.C BYOK).

    The plaintext ``api_key`` is encrypted at rest with Fernet (master
    key from ``TENANT_LLM_FERNET_KEY``). The response NEVER includes
    the key — neither the plaintext nor the ciphertext — so a leaked
    API response cannot be replayed against the production LLM
    provider even if the master key is later compromised.

    Auth: requires admin JWT (per spec §7.2). Cross-tenant access
    returns 404 (anti-enumeration) — mirrors kb-drafts pattern.

    Status codes
    ------------
    * 201 — created or updated.
    * 401 — missing / invalid bearer token (raised by require_admin).
    * 403 — token is not admin / owner role (raised by require_admin).
    * 404 — tenant does not exist OR claims['tenant_id'] != path
      ``tenant_id`` (anti-enumeration — same code either way).
    * 422 — ``provider_name`` not in the allowlist, ``api_key`` empty
      / too long, or ``base_url`` too long. Pydantic's response
      detail does NOT echo the plaintext key.
    """
    if claims.get("tenant_id") != tenant_id:
        # Anti-enumeration: don't reveal that the target tenant exists
        # to an admin of a different tenant. Same response as a real
        # unknown tenant — see the ``ValueError`` branch below.
        raise HTTPException(status_code=404, detail="not found")
    try:
        row = await AdminTenantLLMConfigRepository().upsert(
            tenant_id=tenant_id, payload=payload,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return TenantLLMConfigRead(
        provider_name=row.provider_name,
        base_url=row.base_url,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get(
    "/tenants/{tenant_id}/llm-configs",
    response_model=list[TenantLLMConfigRead],
)
async def list_tenant_llm_configs(
    tenant_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> list[TenantLLMConfigRead]:
    """List a tenant's LLM provider configs.

    Returns ``provider_name`` + ``base_url`` + ``enabled`` +
    ``created_at`` + ``updated_at`` only. The encrypted API key is
    NEVER included in the response — even ciphertext leaks would
    weaken the encryption story if the master key later leaked.

    Auth: requires admin JWT (per spec §7.2). Cross-tenant access
    returns 404 (anti-enumeration) — mirrors kb-drafts pattern.

    Status codes
    ------------
    * 200 — list (possibly empty) of configs.
    * 401 — missing / invalid bearer token (raised by require_admin).
    * 403 — token is not admin / owner role (raised by require_admin).
    * 404 — tenant does not exist OR claims['tenant_id'] != path
      ``tenant_id`` (anti-enumeration — same code either way).
    """
    if claims.get("tenant_id") != tenant_id:
        # Anti-enumeration: same rationale as POST above.
        raise HTTPException(status_code=404, detail="not found")
    try:
        rows = await AdminTenantLLMConfigRepository().list(tenant_id=tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return [
        TenantLLMConfigRead(
            provider_name=r.provider_name,
            base_url=r.base_url,
            enabled=r.enabled,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# M4.D Task 4 — Tenant budget (per-tenant monthly token hard cap) admin endpoints.
#
# POST /api/v1/admin/tenants/{tenant_id}/budget
#   upsert the budget config (soft_warn + hard_cap + period_anchor_tz);
#   no secrets, no encryption.
#
# GET /api/v1/admin/tenants/{tenant_id}/budget
#   read the current config; 404 if not yet configured.
#
# GET /api/v1/admin/tenants/{tenant_id}/budget/usage
#   live snapshot of current-period tokens_used (computed against
#   tenant_budget_snapshots, refreshed on cache miss via SUM(llm_usage)).
#
# Tenant-existence check lives in AdminTenantBudgetRepository —
# 404 is returned for unknown tenant_ids, matching the M4.C
# anti-enumeration pattern.
# ---------------------------------------------------------------------------


@router.post(
    "/tenants/{tenant_id}/budget",
    response_model=TenantBudgetRead,
    status_code=201,
)
async def create_or_update_tenant_budget(
    tenant_id: str,
    payload: TenantBudgetCreate,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> TenantBudgetRead:
    """Upsert a tenant's monthly token budget config.

    Auth: requires admin JWT (per spec §7.2). Cross-tenant access
    returns 404 (anti-enumeration) — mirrors M4.C llm-configs + kb-drafts
    pattern.

    Status codes
    ------------
    * 201 — created or updated.
    * 401 — missing / invalid bearer token (raised by require_admin).
    * 403 — token is not admin / owner role (raised by require_admin).
    * 404 — tenant does not exist OR claims['tenant_id'] != path
      ``tenant_id`` (anti-enumeration — same code either way).
    * 422 — token counts negative or ``period_anchor_tz`` too long
      (Pydantic validation).
    """
    if claims.get("tenant_id") != tenant_id:
        # Anti-enumeration: don't reveal that the target tenant exists
        # to an admin of a different tenant. Same response as a real
        # unknown tenant — see the ``ValueError`` branch below.
        raise HTTPException(status_code=404, detail="not found")
    try:
        row = await AdminTenantBudgetRepository().upsert(
            tenant_id=tenant_id, payload=payload,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return TenantBudgetRead(
        soft_warn_tokens=row.soft_warn_tokens,
        hard_cap_tokens=row.hard_cap_tokens,
        period_anchor_tz=row.period_anchor_tz,
        updated_at=row.updated_at,
    )


@router.get(
    "/tenants/{tenant_id}/budget",
    response_model=TenantBudgetRead,
)
async def get_tenant_budget(
    tenant_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> TenantBudgetRead:
    """Read a tenant's current budget config. 404 if not configured.

    Auth: requires admin JWT. Cross-tenant access returns 404
    (anti-enumeration) — mirrors M4.C pattern.
    """
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    try:
        row = await AdminTenantBudgetRepository().get(tenant_id=tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail="not configured")
    return TenantBudgetRead(
        soft_warn_tokens=row.soft_warn_tokens,
        hard_cap_tokens=row.hard_cap_tokens,
        period_anchor_tz=row.period_anchor_tz,
        updated_at=row.updated_at,
    )


@router.get(
    "/tenants/{tenant_id}/budget/usage",
    response_model=TenantBudgetUsageRead,
)
async def get_tenant_budget_usage(
    tenant_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
    breakdown: bool = Query(default=False, description="Include per-model breakdown"),
) -> TenantBudgetUsageRead:
    """Read the current period's token usage snapshot for a tenant.

    On cache miss the snapshot is refreshed from ``llm_usage`` via
    ``SUM(prompt_tokens + completion_tokens)`` for the period. Works
    even when the tenant has no budget row yet (returns ``None`` for
    soft_warn_tokens / hard_cap_tokens).

    Pack B additions
    ----------------

    * ``effective_cap`` — ``hard_cap_tokens + credits_total``. Always
      included; mirrors the value the resolver pre-check enforces.
    * ``credits_total`` — ``SUM(tenant_budget_credits.tokens)`` for the
      current period. Always 0 when no grants exist.
    * ``breakdown`` — per-provider/per-model token sums, populated only
      when ``?breakdown=true`` is passed. Without the query param the
      field is ``None`` (Pack A callers see no behavioral change).

    Auth: requires admin JWT. Cross-tenant access returns 404
    (anti-enumeration).
    """
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    try:
        budget = await AdminTenantBudgetRepository().get(tenant_id=tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    period, period_start = _current_period(
        budget.period_anchor_tz if budget else "UTC"
    )
    # All read-only Pack B queries share a single async context so the
    # credit SUM and optional breakdown row scans see consistent
    # ``llm_usage`` / ``tenant_budget_credits`` state.
    sm = get_sessionmaker()
    async with sm() as session:
        snap = await TenantBudgetSnapshotRepository().refresh(
            tenant_id=tenant_id, period=period, period_starts_at=period_start,
        )
        # Pack B #2: credits for current period (sum into effective_cap).
        cred_svc = CreditService(session, get_per_model_cache())
        credits_total = await cred_svc.sum_for_period(tenant_id, period)
        # Pack B #5: optional per-model breakdown.
        breakdown_items: list[BreakdownItem] | None = None
        if breakdown:
            pm_svc = PerModelService(session, get_per_model_cache())
            breakdown_items = [
                BreakdownItem(
                    provider=r.provider, model=r.model,
                    prompt_tokens=r.prompt_tokens,
                    completion_tokens=r.completion_tokens,
                    total_tokens=r.total_tokens,
                    request_count=r.request_count,
                )
                for r in await pm_svc.get_breakdown(tenant_id, period)
            ]
    base_cap = budget.hard_cap_tokens if budget else None
    effective_cap = (base_cap or 0) + credits_total
    return TenantBudgetUsageRead(
        period=snap.period,
        tokens_used=snap.tokens_used,
        soft_warn_tokens=budget.soft_warn_tokens if budget else None,
        hard_cap_tokens=base_cap,
        effective_cap=effective_cap,                # NEW (Pack B #2)
        credits_total=credits_total,                # NEW (Pack B #2)
        breakdown=breakdown_items,                  # NEW (Pack B #5)
        period_starts_at=period_start,
    )


# ---------------------------------------------------------------------------
# M4.D Pack A — budget cleanup (tech-debt #1)
#
# POST /api/v1/admin/budget/cleanup
#   Manually trigger the daily retention cleanup. Cross-tenant: only
#   callable by super-admin (no tenant_id in claims).
# ---------------------------------------------------------------------------


@router.post(
    "/budget/cleanup",
    response_model=CleanupResponse,
)
async def trigger_budget_cleanup(
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> CleanupResponse:
    """Manually trigger budget snapshot cleanup (M4.D Pack A #1).

    Cross-tenant: only callable by super-admin (no tenant_id in claims).
    The regular per-tenant admin gets a 404 — anti-enumeration mirrors
    the other admin endpoints in this module.

    Status codes
    ------------
    * 200 — cleanup ran, returns ``{deleted_rows, cutoff_period}``
    * 401 — missing / invalid bearer token (raised by require_admin)
    * 403 — token is not admin / owner role (raised by require_admin)
    * 404 — token has ``tenant_id`` claim (anti-enumeration)
    """
    if claims.get("tenant_id") is not None:
        # Anti-enumeration: don't reveal this endpoint exists to per-tenant
        # admins (the cleanup task affects ALL tenants globally).
        raise HTTPException(status_code=404, detail="not found")
    stats = await run_budget_cleanup()
    return CleanupResponse(
        deleted_rows=stats["deleted_rows"],
        cutoff_period=stats["cutoff_period"],
    )


# ---------------------------------------------------------------------------
# M4.D Pack B #2 — super-admin credit grant + list endpoints.
#
# POST /api/v1/admin/tenants/{tenant_id}/credits
#   insert an immutable audit row (super_admin only — 404 for
#   per-tenant admins via the same anti-enumeration pattern as
#   /budget/cleanup above).
#
# GET /api/v1/admin/tenants/{tenant_id}/credits?period=YYYY-MM
#   list + sum total of credit rows in the requested UTC month.
#
# Anti-enumeration mirrors /budget/cleanup: per-tenant admin tokens
# have ``tenant_id`` set in claims; super-admin tokens have
# ``tenant_id=None`` (via ``create_access_token``'s ``extra`` override)
# and are the only callers that bypass the gate.
#
# Tenant-existence check uses ``TenantRepository().get_by_id`` (NOT
# ``AdminTenantBudgetRepository`` — we don't need a budget row to exist
# to grant credits; a tenant with no budget config can still receive
# credits).
# ---------------------------------------------------------------------------


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
    """Grant a manual token credit to a tenant (Pack B #2).

    Inserts an immutable row into ``tenant_budget_credits`` with the
    super-admin's user id from claims (``claims["sub"]``). The
    effective cap for the current period becomes
    ``hard_cap_tokens + SUM(credits for current period)`` — recomputed
    on the next resolver pre-check.

    Auth: super-admin only. Per-tenant admin tokens (which always
    carry ``tenant_id`` in claims) get a 404 — anti-enumeration
    mirrors ``/budget/cleanup``.

    Status codes
    ------------
    * 201 — credit row inserted; returns the row.
    * 401 — missing / invalid bearer token (raised by require_admin).
    * 403 — token is not admin / owner role (raised by require_admin).
    * 404 — token has ``tenant_id`` claim (anti-enumeration) OR the
      target tenant doesn't exist.
    * 422 — ``tokens <= 0`` or note empty / too long (Pydantic).
    """
    if claims.get("tenant_id") is not None:
        # Anti-enumeration: per-tenant admin can't probe super-admin
        # endpoint existence. Same response as unknown tenant.
        raise HTTPException(status_code=404, detail="not found")
    if await TenantRepository().get_by_id(tenant_id) is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    sm = get_sessionmaker()
    async with sm() as session:
        svc_obj = CreditService(session, get_per_model_cache())
        credit = await svc_obj.grant(
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
    claims: Annotated[dict[str, Any], Depends(require_admin)],
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
) -> CreditListResponse:
    """List credit grants for ``(tenant_id, period)`` (Pack B #2).

    Returns the chronological list of grants + the total tokens
    granted in the period. Used by the admin SPA "Credits" tab to
    audit who got topped up when.

    Auth: super-admin only — same anti-enumeration pattern as POST.

    Status codes
    ------------
    * 200 — list (possibly empty) of grants + total.
    * 401 / 403 — auth gate.
    * 404 — token has ``tenant_id`` claim OR the target tenant
      doesn't exist.
    * 422 — ``period`` doesn't match ``YYYY-MM`` (Pydantic / FastAPI
      Query regex).
    """
    if claims.get("tenant_id") is not None:
        raise HTTPException(status_code=404, detail="not found")
    if await TenantRepository().get_by_id(tenant_id) is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    sm = get_sessionmaker()
    async with sm() as session:
        svc_obj = CreditService(session, get_per_model_cache())
        rows = await svc_obj.list_for_period(tenant_id, period)
        total = await svc_obj.sum_for_period(tenant_id, period)
    return CreditListResponse(
        credits=[CreditResponse.model_validate(r) for r in rows],
        total_tokens=total,
    )


__all__ = ["router"]