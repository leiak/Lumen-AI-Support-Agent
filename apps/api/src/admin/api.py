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
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

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
from admin.schemas.dashboard import DashboardRead, StatusCount
from admin.schemas.llm_chain import (
    LLMChainRead,
    LLMChainStep,
    LLMChainUpdate,
    LLMProviderInfo,
)
from admin.schemas.search import SearchResultsOut
from admin.schemas.tenant_llm_config import (
    TenantLLMConfigCreate,
    TenantLLMConfigRead,
    TenantLLMConfigUpdate,
)
from admin.schemas.tenant_settings import (
    TenantSettingsRead,
    TenantSettingsUpdate,
)
from admin.schemas.users import (
    UserCreate,
    UserPasswordReset,
    UserRead,
    UserUpdate,
)
from admin.schemas.widget_config import (
    WidgetConfigRead,
    WidgetConfigResetResponse,
    WidgetConfigUpdate,
)
from admin.search import AdminSearchService
from auth.dependencies import require_admin
from auth.password import hash_password
from budget.cleanup import run_budget_cleanup
from budget.credits import CreditService
from budget.per_model import PerModelService, get_per_model_cache
from budget.repository import TenantBudgetSnapshotRepository
from budget.resolver import _current_period
from conversation.enums import ConversationStatus
from conversation.models import Conversation
from core.config import get_settings
from core.database import get_sessionmaker
from core.id_gen import new_id
from knowledge.enums import ArticleSourceType, ArticleStatus
from knowledge.models import (
    Article,
    ArticleVersion,
    KbArticleDraft,
    KnowledgeBase,
)
from llm_client.models import LLMUsage
from llm_client.provider_registry import build_provider_registry
from llm_client.runtime_chain import (
    ChainStep,
    get_runtime_chain,
)
from tenant.models import User
from tenant.repository import TenantRepository, UserRepository
from ticket.enums import TicketPriority, TicketStatus
from ticket.models import SlaPolicy, Ticket
from ticket.repository import TicketRepository
from ticket.schemas import (
    SlaPolicyCreate,
    SlaPolicyRead,
    SlaPolicyUpdate,
    TicketListRead,
    TicketOut,
)
from widget.repository import WidgetConfigRepository

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
        draft.reviewed_at = datetime.now(UTC)
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
        draft.reviewed_at = datetime.now(UTC)
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


@router.get(
    "/tenants/{tenant_id}/sla-policies",
    response_model=list[SlaPolicyRead],
)
async def list_sla_policies(
    tenant_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> list[SlaPolicyRead]:
    """List the tenant's SLA policies (one per priority bucket).

    Read-only; policies are configured by tenant admins out-of-band
    (seed script / DB migration). Returns rows ordered by priority
    (P0 → P3) so the admin SPA can render them as a stable table.

    Auth: requires admin JWT. Cross-tenant access returns 404
    (anti-enumeration) — mirrors the kb-drafts / llm-configs /
    budget pattern in this module.

    Status codes
    ------------
    * 200 — list (possibly empty) of policies, ordered by priority.
    * 401 — missing / invalid bearer token (raised by require_admin).
    * 403 — token is not admin / owner role (raised by require_admin).
    * 404 — claims['tenant_id'] != path ``tenant_id`` (anti-enumeration).
    """
    if claims.get("tenant_id") != tenant_id:
        # Anti-enumeration: don't reveal that the target tenant exists
        # to an admin of a different tenant. Same response as a real
        # unknown tenant.
        raise HTTPException(status_code=404, detail="not found")
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(
            select(SlaPolicy)
            .where(SlaPolicy.tenant_id == tenant_id)
            .order_by(SlaPolicy.priority)
        )
        return [SlaPolicyRead.model_validate(p) for p in result.scalars()]


# ---------------------------------------------------------------------------
# Tier 2 Task 2.1 — SLA policy mutation endpoints (POST / PATCH / DELETE).
#
# POST   /api/v1/admin/tenants/{tenant_id}/sla-policies
#   upsert on (tenant_id, name); Pydantic enforces uniqueness at the
#   column level — the SPA names policies freely but can't collide on
#   names within a tenant.
#
# PATCH  /api/v1/admin/tenants/{tenant_id}/sla-policies/{policy_id}
#   update mutable fields. ``name`` is the natural key and CANNOT be
#   changed here — rename requires delete + recreate.
#
# DELETE /api/v1/admin/tenants/{tenant_id}/sla-policies/{policy_id}
#   idempotent. Tickets that reference the deleted policy keep their
#   stamped ``sla_deadline_at`` (it's a timestamp, not a FK) so
#   historical data is intact.
# ---------------------------------------------------------------------------


@router.post(
    "/tenants/{tenant_id}/sla-policies",
    response_model=SlaPolicyRead,
    status_code=201,
)
async def create_sla_policy(
    tenant_id: str,
    payload: SlaPolicyCreate,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> SlaPolicyRead:
    """Upsert an SLA policy for the tenant (Tier 2 Task 2.1).

    ``(tenant_id, name)`` is unique; re-using a name overwrites the
    existing row's mutable fields (priority, first_response_minutes,
    resolution_minutes, business_hours_only). The ``id`` (ULID) is
    regenerated on overwrite so existing FK references from tickets
    keep pointing at a valid historical record.

    Auth: requires admin JWT. Cross-tenant access returns 404.
    """
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    sm = get_sessionmaker()
    async with sm() as session:
        existing = (
            await session.execute(
                select(SlaPolicy).where(
                    SlaPolicy.tenant_id == tenant_id,
                    SlaPolicy.name == payload.name,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.priority = payload.priority
            existing.first_response_minutes = payload.first_response_minutes
            existing.resolution_minutes = payload.resolution_minutes
            existing.business_hours_only = payload.business_hours_only
            row = existing
        else:
            row = SlaPolicy(
                id=new_id(),
                tenant_id=tenant_id,
                name=payload.name,
                priority=payload.priority,
                first_response_minutes=payload.first_response_minutes,
                resolution_minutes=payload.resolution_minutes,
                business_hours_only=payload.business_hours_only,
            )
            session.add(row)
        await session.flush()
        await session.commit()
        await session.refresh(row)
        return SlaPolicyRead.model_validate(row)


@router.patch(
    "/tenants/{tenant_id}/sla-policies/{policy_id}",
    response_model=SlaPolicyRead,
)
async def patch_sla_policy(
    tenant_id: str,
    policy_id: str,
    payload: SlaPolicyUpdate,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> SlaPolicyRead:
    """Update an SLA policy's mutable fields (Tier 2 Task 2.1).

    Returns 404 for cross-tenant / unknown-policy / unknown-tenant —
    all three collapse to the same response per anti-enumeration.

    Auth: requires admin JWT.
    """
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    sm = get_sessionmaker()
    async with sm() as session:
        existing = (
            await session.execute(
                select(SlaPolicy).where(
                    SlaPolicy.tenant_id == tenant_id,
                    SlaPolicy.id == policy_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise HTTPException(status_code=404, detail="not found")
        if payload.priority is not None:
            existing.priority = payload.priority
        if payload.first_response_minutes is not None:
            existing.first_response_minutes = payload.first_response_minutes
        if payload.resolution_minutes is not None:
            existing.resolution_minutes = payload.resolution_minutes
        if payload.business_hours_only is not None:
            existing.business_hours_only = payload.business_hours_only
        await session.flush()
        await session.commit()
        await session.refresh(existing)
        return SlaPolicyRead.model_validate(existing)


@router.delete(
    "/tenants/{tenant_id}/sla-policies/{policy_id}",
    status_code=204,
)
async def delete_sla_policy(
    tenant_id: str,
    policy_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> Response:
    """Delete an SLA policy. Idempotent (Tier 2 Task 2.1).

    Tickets that referenced this policy keep their stamped
    ``sla_deadline_at`` (it's a snapshot, not a FK). The operator can
    re-create the policy with the same name via POST.

    Auth: requires admin JWT.
    """
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    sm = get_sessionmaker()
    async with sm() as session:
        stmt = delete(SlaPolicy).where(
            SlaPolicy.tenant_id == tenant_id,
            SlaPolicy.id == policy_id,
        )
        result = await session.execute(stmt)
        await session.commit()
        # 204 whether the row existed or not — idempotent.
        if (result.rowcount or 0) == 0:
            # Re-check whether the row exists at all. If we hit zero
            # rows it's because the tenant has no such policy; that's
            # still 204 by idempotency. We don't surface 404 here.
            pass
    return Response(status_code=204)


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
# Tier 1 Task 1.3 — LLM config mutation endpoints.
#
# PATCH /api/v1/admin/tenants/{tenant_id}/llm-configs/{provider_name}
#   toggle ``enabled`` and/or adjust ``base_url`` without rotating the
#   plaintext API key. The encrypted key is preserved across the call.
#
# DELETE /api/v1/admin/tenants/{tenant_id}/llm-configs/{provider_name}
#   remove the (tenant, provider) row. Idempotent — the operator can
#   always re-add the provider with POST afterwards.
#
# Both endpoints follow the same anti-enumeration + 404 pattern as
# the POST / GET pair above.
# ---------------------------------------------------------------------------


@router.patch(
    "/tenants/{tenant_id}/llm-configs/{provider_name}",
    response_model=TenantLLMConfigRead,
)
async def patch_tenant_llm_config(
    tenant_id: str,
    provider_name: str,
    payload: TenantLLMConfigUpdate,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> TenantLLMConfigRead:
    """Update ``enabled`` and/or ``base_url`` for an existing provider config.

    Tier 1 Task 1.3. The plaintext API key is NEVER touched — only the
    mutable sidecar fields. Response shape matches GET: the API key is
    excluded entirely (not even the Fernet ciphertext).

    Auth: requires admin JWT. Cross-tenant access returns 404.

    Status codes
    ------------
    * 200 — updated.
    * 404 — tenant does not exist OR cross-tenant OR no row for the
      (tenant, provider) pair. Same code for all three cases — see
      ``Anti-enumeration`` note below.
    * 422 — ``base_url`` too long (Pydantic).

    Anti-enumeration
    ----------------
    The three 404 paths (unknown tenant / cross-tenant / unknown
    provider) all return the same body so an attacker can't probe
    which (tenant, provider) pairs exist.
    """
    if claims.get("tenant_id") != tenant_id:
        # cross-tenant: same 404 as the unknown-tenant / unknown-
        # provider branches below.
        raise HTTPException(status_code=404, detail="not found")
    try:
        row = await AdminTenantLLMConfigRepository().update(
            tenant_id=tenant_id,
            provider_name=provider_name,
            payload=payload,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return TenantLLMConfigRead(
        provider_name=row.provider_name,
        base_url=row.base_url,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.delete(
    "/tenants/{tenant_id}/llm-configs/{provider_name}",
    status_code=204,
)
async def delete_tenant_llm_config(
    tenant_id: str,
    provider_name: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> Response:
    """Remove the (tenant, provider) LLM config row.

    Tier 1 Task 1.3. Idempotent — ``False`` (no row to delete) is
    treated the same as ``True`` (success) and returns 204. The
    anti-enumeration gate mirrors PATCH above: unknown tenant /
    cross-tenant / unknown provider all collapse to the same 404.

    Status codes
    ------------
    * 204 — deleted (or nothing to delete — still 204).
    * 404 — tenant does not exist OR cross-tenant. Same code for
      both. The PATCH path also collapses unknown provider here.
    """
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    try:
        await AdminTenantLLMConfigRepository().delete(
            tenant_id=tenant_id, provider_name=provider_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    # Idempotent: True (deleted) and False (nothing to delete) both
    # return 204. Only tenant-existence failures surface as 404.
    return Response(status_code=204)


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


# ---------------------------------------------------------------------------
# Tier 1 Task 1.1 — Admin ticket list endpoint.
#
# GET /api/v1/admin/tenants/{tenant_id}/tickets
#   List the tenant's tickets, newest-first. Optional filters:
#     * status    — exact match (one of the 7 TicketStatus values)
#     * priority  — exact match (one of P0/P1/P2/P3)
#     * limit     — page size, 1..200, default 50
#     * offset    — page offset, >= 0, default 0
#   Response is a ``TicketListRead`` envelope: ``{items, total}``.
#
# Tenant-scoped on the WHERE clause; JWT tenant check at the boundary
# is the standard anti-enumeration mirror of the budget / kb-drafts /
# llm-configs routes above. Returns 404 (NOT 403) when ``claims.tenant_id``
# does not match the path tenant — cross-tenant probing yields the
# same status code as a missing / unknown tenant.
# ---------------------------------------------------------------------------


@router.get(
    "/tenants/{tenant_id}/tickets",
    response_model=TicketListRead,
)
async def list_tenant_tickets(
    tenant_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
    status: str | None = Query(
        None,
        description="Filter by ticket status (e.g. 'new', 'triaged', 'resolved')",
    ),
    priority: str | None = Query(
        None,
        description="Filter by priority (one of P0/P1/P2/P3)",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> TicketListRead:
    """List tickets for the tenant, newest-first.

    Returns a ``TicketListRead`` envelope (``items`` + ``total``) so
    the SPA can render a paginator without an extra round-trip. Both
    ``status`` and ``priority`` are optional; ``None`` means
    "no filter". The same filter set is applied to the count query
    so ``total`` reflects what the user sees on the current filter.

    Auth: admin JWT required. Cross-tenant access returns 404
    (anti-enumeration) — mirrors the rest of this module.

    Status codes
    ------------
    * 200 — list (possibly empty) of tickets + total count.
    * 401 — missing / invalid bearer token (raised by ``require_admin``).
    * 403 — token is not admin / owner role (raised by ``require_admin``).
    * 404 — ``claims['tenant_id']`` != path ``tenant_id`` (anti-enumeration).
    * 422 — ``status`` / ``priority`` not a valid enum value, or
      ``limit`` / ``offset`` out of range (Pydantic / FastAPI Query).
    """
    if claims.get("tenant_id") != tenant_id:
        # Anti-enumeration: don't reveal that the target tenant exists
        # to an admin of a different tenant. Same response as a real
        # unknown tenant — matches the llm-configs + budget + sla-policies
        # routes above.
        raise HTTPException(status_code=404, detail="not found")
    # Parse enum query params inside the route body so a malformed
    # value surfaces as a clean 422 from Pydantic instead of a 500
    # from the enum() call.
    status_enum = TicketStatus(status) if status else None
    priority_enum = TicketPriority(priority) if priority else None
    sm = get_sessionmaker()
    async with sm() as session:
        repo = TicketRepository(session)
        items = await repo.list_by_tenant(
            tenant_id,
            status=status_enum,
            priority=priority_enum,
            limit=limit,
            offset=offset,
        )
        total = await repo.count_by_tenant(
            tenant_id,
            status=status_enum,
            priority=priority_enum,
        )
    return TicketListRead(
        items=[TicketOut.model_validate(t) for t in items],
        total=total,
    )


# ---------------------------------------------------------------------------
# Tier 2 Task 2.3 — Tenant self-settings endpoints.
#
# GET   /api/v1/admin/tenants/{tid}/settings — read name/plan/status
# PATCH /api/v1/admin/tenants/{tid}/settings — update name/plan/status
#
# Scope: a tenant-scoped admin can view + edit their own tenant.
# Cross-tenant access returns 404 (anti-enumeration, same as the rest
# of the admin surface). Listing all tenants would require a super-
# admin role that does NOT exist in the current JWT model; that's
# deliberately out of scope for this task.
# ---------------------------------------------------------------------------


@router.get(
    "/tenants/{tenant_id}/settings",
    response_model=TenantSettingsRead,
)
async def get_tenant_settings(
    tenant_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> TenantSettingsRead:
    """Read the current tenant's settings."""
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    tenant = await TenantRepository().get_by_id(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="not found")
    return TenantSettingsRead.model_validate(tenant)


@router.patch(
    "/tenants/{tenant_id}/settings",
    response_model=TenantSettingsRead,
)
async def patch_tenant_settings(
    tenant_id: str,
    payload: TenantSettingsUpdate,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> TenantSettingsRead:
    """Update the current tenant's mutable settings.

    All fields optional. ``status`` is restricted to ``active`` /
    ``suspended`` — ``deleted`` is reserved for a future super-admin
    path (out of scope for the tenant-scoped admin surface).
    """
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    if payload.status is not None and payload.status.value == "deleted":
        raise HTTPException(
            status_code=400,
            detail="deletion requires super-admin (not exposed via this endpoint)",
        )
    updated = await TenantRepository().update_profile(
        tenant_id,
        name=payload.name,
        plan=payload.plan,
        status=payload.status,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="not found")
    return TenantSettingsRead.model_validate(updated)


# ---------------------------------------------------------------------------
# Tier 2 Task 2.2 — Tenant user (agent / admin) CRUD endpoints.
#
# GET    /api/v1/admin/tenants/{tenant_id}/users              — list
# POST   /api/v1/admin/tenants/{tenant_id}/users              — create (bcrypt-hash pw)
# PATCH  /api/v1/admin/tenants/{tenant_id}/users/{user_id}    — role / full_name / is_active
# POST   /api/v1/admin/tenants/{tenant_id}/users/{user_id}/reset-password
# DELETE /api/v1/admin/tenants/{tenant_id}/users/{user_id}    — idempotent hard delete
#
# Anti-enumeration: a caller whose JWT tenant_id does not match
# {tenant_id} gets 404. The handler also blocks deleting the
# operator's own row (self-delete → 400) so an admin can't accidentally
# lock themselves out.
# ---------------------------------------------------------------------------


@router.get(
    "/tenants/{tenant_id}/users",
    response_model=list[UserRead],
)
async def list_tenant_users(
    tenant_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> list[UserRead]:
    """List all users belonging to a tenant.

    Returns ``UserRead`` (no ``password_hash``). Ordered by
    ``created_at`` ascending so the SPA renders a stable table.
    Cross-tenant access returns 404 (anti-enumeration).
    """
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    users = await UserRepository().list_for_tenant(tenant_id)
    return [UserRead.model_validate(u) for u in users]


@router.post(
    "/tenants/{tenant_id}/users",
    response_model=UserRead,
    status_code=201,
)
async def create_tenant_user(
    tenant_id: str,
    payload: UserCreate,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> UserRead:
    """Create a new tenant user.

    Password is bcrypt-hashed via :func:`auth.password.hash_password`
    before insert. Returns 409 on duplicate (tenant_id, email)
    (mapped from ``IntegrityError``). Returns 404 on cross-tenant
    access (anti-enumeration).
    """
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    repo = UserRepository()
    try:
        user = await repo.create(
            tenant_id=tenant_id,
            email=payload.email,
            password_hash=hash_password(payload.password),
            full_name=payload.full_name,
            role=payload.role,
            is_active=True,
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="email already exists",
        ) from exc
    return UserRead.model_validate(user)


@router.patch(
    "/tenants/{tenant_id}/users/{user_id}",
    response_model=UserRead,
)
async def patch_tenant_user(
    tenant_id: str,
    user_id: str,
    payload: UserUpdate,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> UserRead:
    """Update a tenant user's mutable profile fields.

    All fields optional. Cross-tenant access returns 404; the user
    belonging to a different tenant is also 404 (anti-enumeration).
    """
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    repo = UserRepository()
    existing = await repo.get_by_id(user_id)
    if existing is None or existing.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    updated = await repo.update_profile(
        user_id,
        full_name=payload.full_name,
        role=payload.role,
        is_active=payload.is_active,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="not found")
    return UserRead.model_validate(updated)


@router.post(
    "/tenants/{tenant_id}/users/{user_id}/reset-password",
    status_code=204,
)
async def reset_tenant_user_password(
    tenant_id: str,
    user_id: str,
    payload: UserPasswordReset,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> Response:
    """Reset a tenant user's password.

    Tier 2 Task 2.2 — admin-triggered password reset. ``password_hash``
    is never returned in the response body (204 No Content). Cross-
    tenant or unknown user → 404 (anti-enumeration).
    """
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    repo = UserRepository()
    existing = await repo.get_by_id(user_id)
    if existing is None or existing.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    await repo.update_password_hash(user_id, hash_password(payload.new_password))
    return Response(status_code=204)


@router.delete(
    "/tenants/{tenant_id}/users/{user_id}",
    status_code=204,
)
async def delete_tenant_user(
    tenant_id: str,
    user_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> Response:
    """Hard-delete a tenant user. Idempotent.

    Tier 2 Task 2.2. Self-delete is rejected with 400 so an admin
    can't accidentally lock themselves out — the UI MUST prompt
    confirmation and surface a 400 as "can't delete yourself".
    FK references from tickets / audit logs will raise
    ``IntegrityError``; we map that to 409 so the caller knows it's
    a data-conflict rather than a permission error.
    """
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    if user_id == claims.get("sub"):
        raise HTTPException(
            status_code=400,
            detail="cannot delete the currently authenticated user",
        )
    repo = UserRepository()
    existing = await repo.get_by_id(user_id)
    if existing is None or existing.tenant_id != tenant_id:
        # Idempotent — return 204 even when there's nothing to delete.
        return Response(status_code=204)
    try:
        await repo.delete_by_id(user_id)
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="user is referenced by other rows",
        ) from exc
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Tier 2 Task 2.4 — Tenant widget appearance config endpoints.
#
# GET    /api/v1/admin/tenants/{tid}/widget-config   — read (returns defaults if absent)
# PATCH  /api/v1/admin/tenants/{tid}/widget-config   — upsert (creates row with defaults)
# DELETE /api/v1/admin/tenants/{tid}/widget-config   — reset to defaults (idempotent)
#
# Anti-enumeration 404 (cross-tenant access), same as the rest of the
# admin surface. Defaults match the M2.A baseline so a freshly-
# seeded tenant looks identical before any admin edits.
# ---------------------------------------------------------------------------


@router.get(
    "/tenants/{tenant_id}/widget-config",
    response_model=WidgetConfigRead,
)
async def get_widget_config(
    tenant_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> WidgetConfigRead:
    """Read the current tenant's widget appearance config.

    If the row is absent (tenant has never customized), the
    response materializes the M2.A defaults — the SPA renders an
    editable form pre-filled with those values, and the first PATCH
    materializes the row.
    """
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    row = await WidgetConfigRepository().get(tenant_id)
    if row is None:
        # Return a synthesized default row (no DB write).
        from admin.schemas.widget_config import (
            DEFAULT_PRIMARY_COLOR,
            DEFAULT_WELCOME_MESSAGE,
            DEFAULT_WIDGET_TITLE,
        )

        return WidgetConfigRead(
            tenant_id=tenant_id,
            theme="light",
            primary_color=DEFAULT_PRIMARY_COLOR,
            position="bottom-right",
            widget_title=DEFAULT_WIDGET_TITLE,
            welcome_message=DEFAULT_WELCOME_MESSAGE,
            logo_url=None,
            enabled=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    return WidgetConfigRead.model_validate(row)


@router.patch(
    "/tenants/{tenant_id}/widget-config",
    response_model=WidgetConfigRead,
)
async def patch_widget_config(
    tenant_id: str,
    payload: WidgetConfigUpdate,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> WidgetConfigRead:
    """Upsert the tenant's widget appearance config.

    All fields optional. PATCH on an absent row creates one with
    the server-side defaults; omitted fields preserve the existing
    value.
    """
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    row = await WidgetConfigRepository().upsert(
        tenant_id,
        theme=payload.theme,
        primary_color=payload.primary_color,
        position=payload.position,
        widget_title=payload.widget_title,
        welcome_message=payload.welcome_message,
        logo_url=payload.logo_url,
        enabled=payload.enabled,
    )
    return WidgetConfigRead.model_validate(row)


@router.delete(
    "/tenants/{tenant_id}/widget-config",
    response_model=WidgetConfigResetResponse,
)
async def reset_widget_config(
    tenant_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> WidgetConfigResetResponse:
    """Reset the tenant's widget config to defaults (idempotent).

    The next GET will materialize the defaults in memory — there is
    no need to re-insert a row until the admin PATCHes again.
    """
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    await WidgetConfigRepository().delete(tenant_id)
    return WidgetConfigResetResponse(tenant_id=tenant_id, reset=True)


# ---------------------------------------------------------------------------
# Tier 2 Task 2.5 — Admin Dashboard aggregate endpoint.
#
# GET /api/v1/admin/tenants/{tid}/dashboard — tickets today + by status +
#   by priority + open conversations + LLM tokens this period + user count.
#
# Anti-enumeration 404 on cross-tenant access, same as the rest of the
# admin surface. The endpoint is read-only and idempotent — the SPA
# polls it (or refetches on focus) for live KPI tiles.
# ---------------------------------------------------------------------------


@router.get(
    "/tenants/{tenant_id}/dashboard",
    response_model=DashboardRead,
)
async def get_tenant_dashboard(
    tenant_id: str,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> DashboardRead:
    """Aggregate KPI counts for the admin dashboard page.

    Returns ticket + conversation + LLM-usage + user counts scoped
    to ``tenant_id``. ``period`` / ``period_starts_at`` mirror the
    budget snapshot so the operator sees the same window the cap
    applies to.
    """
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="not found")

    # ---- tickets ----
    today_utc_start = datetime.now(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    sm = get_sessionmaker()
    async with sm() as session:
        # Tickets today (UTC midnight onward).
        tickets_today_q = await session.execute(
            select(func.count(Ticket.id)).where(
                Ticket.tenant_id == tenant_id,
                Ticket.created_at >= today_utc_start,
            )
        )
        tickets_today = int(tickets_today_q.scalar() or 0)

        # Tickets by status.
        tickets_by_status_rows = await session.execute(
            select(Ticket.status, func.count(Ticket.id))
            .where(Ticket.tenant_id == tenant_id)
            .group_by(Ticket.status)
        )
        tickets_by_status = [
            StatusCount(key=str(row[0].value), count=int(row[1]))
            for row in tickets_by_status_rows.all()
        ]

        # Tickets by priority.
        tickets_by_priority_rows = await session.execute(
            select(Ticket.priority, func.count(Ticket.id))
            .where(Ticket.tenant_id == tenant_id)
            .group_by(Ticket.priority)
        )
        tickets_by_priority = [
            StatusCount(key=str(row[0].value), count=int(row[1]))
            for row in tickets_by_priority_rows.all()
        ]

        # Open conversations — status=open OR pending both require
        # operator attention, so both count toward the dashboard
        # "open" tile.
        conv_open_q = await session.execute(
            select(func.count(Conversation.id)).where(
                Conversation.tenant_id == tenant_id,
                Conversation.status.in_(
                    [ConversationStatus.OPEN, ConversationStatus.PENDING]
                ),
            )
        )
        conversations_open = int(conv_open_q.scalar() or 0)

        # LLM tokens this period — anchored to UTC month for the
        # dashboard. The Budget page reads the budget endpoint for
        # the tenant-configured TZ; the dashboard uses UTC for
        # simplicity (and so the operator doesn't see two different
        # "this month" numbers on adjacent tiles).
        period, period_starts_at = _current_period("UTC")
        # M4.D Pack B #5: LLMUsage.created_at is stored TIMESTAMP WITHOUT
        # TIME ZONE — strip tzinfo before comparing so asyncpg doesn't
        # try to subtract offset-naive vs offset-aware datetimes.
        period_starts_naive = (
            period_starts_at.replace(tzinfo=None)
            if period_starts_at.tzinfo is not None
            else period_starts_at
        )
        tokens_q = await session.execute(
            select(
                func.coalesce(
                    func.sum(
                        LLMUsage.prompt_tokens + LLMUsage.completion_tokens
                    ),
                    0,
                )
            ).where(
                LLMUsage.tenant_id == tenant_id,
                LLMUsage.created_at >= period_starts_naive,
            )
        )
        llm_tokens_this_period = int(tokens_q.scalar() or 0)

        # User counts.
        active_users_q = await session.execute(
            select(func.count(User.id)).where(
                User.tenant_id == tenant_id, User.is_active.is_(True)
            )
        )
        active_users = int(active_users_q.scalar() or 0)
        total_users_q = await session.execute(
            select(func.count(User.id)).where(
                User.tenant_id == tenant_id
            )
        )
        total_users = int(total_users_q.scalar() or 0)

    return DashboardRead(
        today_utc_start=today_utc_start,
        tickets_today=tickets_today,
        tickets_by_status=tickets_by_status,
        tickets_by_priority=tickets_by_priority,
        conversations_open=conversations_open,
        period=period,
        period_starts_at=period_starts_at,
        llm_tokens_this_period=llm_tokens_this_period,
        active_users=active_users,
        total_users=total_users,
    )


# ---------------------------------------------------------------------------
# Tier 3 Task 3.3 — cross-domain search
# ---------------------------------------------------------------------------


@router.get(
    "/search",
    response_model=SearchResultsOut,
)
async def admin_search(
    claims: Annotated[dict[str, Any], Depends(require_admin)],
    q: Annotated[str, Query(min_length=1, max_length=100)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> SearchResultsOut:
    """Cross-domain search across KB articles + tickets + conversations.

    The tenant is taken from the JWT — operators can only search
    what they're already authorised to read. Empty ``q`` returns
    an empty result set (not an error) so a cleared input doesn't
    flash a 422 to the user.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        service = AdminSearchService(session)
        return await service.search(
            tenant_id=claims["tenant_id"],
            query=q,
            limit=limit,
        )


# ---------------------------------------------------------------------------
# Tier 3 Task 3.4 — runtime LLM provider chain admin
# ---------------------------------------------------------------------------


def _list_global_providers() -> list[LLMProviderInfo]:
    """Snapshot the globally-registered providers (no credentials)."""
    try:
        registry = build_provider_registry(get_settings())
    except RuntimeError:
        # No provider configured — return empty list so the SPA
        # shows an explicit "no providers" empty state instead of
        # crashing the page.
        return []
    default_name = next(iter(registry))
    return [
        LLMProviderInfo(
            name=p.name,
            model=getattr(p, "model", ""),
            is_default=(p.name == default_name),
        )
        for p in registry.values()
    ]


@router.get(
    "/llm-chain",
    response_model=LLMChainRead,
)
async def get_llm_chain(
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> LLMChainRead:
    """Read the current runtime fallback chain + provider registry.

    Returns the chain that the next gateway construction will use
    (the ``RuntimeChain`` singleton), plus the list of providers
    the admin is allowed to put in the chain. No API keys are
    included — the SPA doesn't need them.
    """
    runtime = get_runtime_chain()
    steps = [
        LLMChainStep(provider=s.provider, model=s.model, enabled=s.enabled)
        for s in runtime.snapshot()
    ]
    return LLMChainRead(steps=steps, providers=_list_global_providers())


@router.put(
    "/llm-chain",
    response_model=LLMChainRead,
)
async def put_llm_chain(
    payload: LLMChainUpdate,
    claims: Annotated[dict[str, Any], Depends(require_admin)],
) -> LLMChainRead:
    """Replace the runtime fallback chain atomically.

    Validation rules:
    * Each ``provider`` must be in the registered global providers.
    * The resulting chain must have at least one ``enabled`` step
      (an empty / fully-disabled chain is rejected so the gateway
      can't accidentally fall back to prefix-only routing).

    Successful updates take effect on the next gateway construction
    (the per-tenant LLM cache picks them up). Already-cached
    resolvers keep their old chain until they expire.
    """
    providers = _list_global_providers()
    known = {p.name for p in providers}
    try:
        await get_runtime_chain().replace_steps(
            [ChainStep(s.provider, s.model, s.enabled) for s in payload.steps],
            known_providers=known,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    runtime = get_runtime_chain()
    steps = [
        LLMChainStep(provider=s.provider, model=s.model, enabled=s.enabled)
        for s in runtime.snapshot()
    ]
    return LLMChainRead(steps=steps, providers=providers)


__all__ = ["router"]