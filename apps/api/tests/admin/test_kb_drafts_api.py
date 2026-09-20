"""Integration tests for admin KB drafts API.

Stage 18 / M2.B Task 8.

Coverage:

* ``GET  /admin/kb-drafts`` returns only the caller's drafts.
* ``POST /admin/kb-drafts/{id}/approve`` creates an Article in the
  tenant's auto-mined KB + marks the draft APPROVED.
* ``POST /admin/kb-drafts/{id}/reject`` marks the draft REJECTED
  with no Article created.
* Cross-tenant access: list returns empty for the foreign tenant;
  approve/reject on a foreign draft returns 404.
* Already-APPROVED / already-REJECTED drafts → 409 Conflict.
"""
from __future__ import annotations

import pytest
from core.database import get_sessionmaker
from core.id_gen import new_id
from httpx import AsyncClient
from knowledge.models import Article, KbArticleDraft
from sqlalchemy import select
from tenant.models import Tenant


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_drafts_returns_only_tenant_drafts(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    """List endpoint returns only the requesting tenant's drafts."""
    sm = get_sessionmaker()
    async with sm() as session:
        for i in range(3):
            session.add(
                KbArticleDraft(
                    id=new_id(),
                    tenant_id=sample_tenant.id,
                    cluster_id=i,
                    source_questions=["q1", "q2"],
                    suggested_title=f"Draft {i}",
                    suggested_body="body",
                    suggested_tags=[],
                    status="DRAFT",
                )
            )
        await session.commit()

    resp = await async_client.get(
        "/api/v1/admin/kb-drafts",
        params={"tenant_id": sample_tenant.id},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["drafts"]) == 3
    # Listing exposes body_preview only — never raw source_question IDs.
    for d in body["drafts"]:
        assert "source_questions" not in d
        assert d["source_question_count"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_drafts_filters_by_status(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    """Status filter narrows the result set."""
    sm = get_sessionmaker()
    async with sm() as session:
        for status, title in (("DRAFT", "Draft"), ("REJECTED", "Rejected")):
            session.add(
                KbArticleDraft(
                    id=new_id(),
                    tenant_id=sample_tenant.id,
                    cluster_id=0,
                    source_questions=["q"],
                    suggested_title=f"{title} {status}",
                    suggested_body="b",
                    suggested_tags=[],
                    status=status,
                )
            )
        await session.commit()

    resp = await async_client.get(
        "/api/v1/admin/kb-drafts",
        params={"tenant_id": sample_tenant.id, "status": "DRAFT"},
    )
    body = resp.json()
    assert len(body["drafts"]) == 1
    assert body["drafts"][0]["status"] == "DRAFT"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_draft_returns_full_body(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    """GET detail endpoint returns full body (not just 200-char preview).

    Without this endpoint, admins literally cannot review a draft
    end-to-end (list returns only body_preview[:200]).
    """
    sm = get_sessionmaker()
    draft_id = new_id()
    long_body = "x" * 500
    async with sm() as session:
        session.add(
            KbArticleDraft(
                id=draft_id,
                tenant_id=sample_tenant.id,
                cluster_id=0,
                source_questions=["q1"],
                suggested_title="T",
                suggested_body=long_body,
                suggested_tags=[],
                status="DRAFT",
            )
        )
        await session.commit()

    resp = await async_client.get(
        f"/api/v1/admin/kb-drafts/{draft_id}",
        params={"tenant_id": sample_tenant.id},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["body"] == long_body  # Full body returned, not truncated
    assert body["title"] == "T"
    assert body["status"] == "DRAFT"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_draft_cross_tenant_returns_404(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    """GET detail cross-tenant returns 404 (anti-enumeration)."""
    sm = get_sessionmaker()
    draft_id = new_id()
    other_tenant_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    async with sm() as session:
        session.add(
            KbArticleDraft(
                id=draft_id,
                tenant_id=other_tenant_id,
                cluster_id=0,
                source_questions=["q"],
                suggested_title="Foreign",
                suggested_body="b",
                suggested_tags=[],
                status="DRAFT",
            )
        )
        await session.commit()

    resp = await async_client.get(
        f"/api/v1/admin/kb-drafts/{draft_id}",
        params={"tenant_id": sample_tenant.id},
    )
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approve_draft_creates_article_and_marks_approved(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    """Approve → Article created in auto-mined KB + draft status=APPROVED."""
    sm = get_sessionmaker()
    draft_id = new_id()
    async with sm() as session:
        session.add(
            KbArticleDraft(
                id=draft_id,
                tenant_id=sample_tenant.id,
                cluster_id=0,
                source_questions=["q1"],
                suggested_title="Reset Password",
                suggested_body="Go to settings > account > reset password.",
                suggested_tags=["account"],
                status="DRAFT",
            )
        )
        await session.commit()

    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{draft_id}/approve",
        params={"tenant_id": sample_tenant.id, "reviewer_id": "admin1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "APPROVED"
    assert body["article_id"]

    # Side-effects: Article row exists + draft.status='APPROVED' + link
    async with sm() as session:
        article = await session.get(Article, body["article_id"])
        assert article is not None
        assert article.tenant_id == sample_tenant.id
        assert article.title == "Reset Password"

        draft_row = (
            await session.execute(
                select(KbArticleDraft).where(KbArticleDraft.id == draft_id)
            )
        ).scalar_one()
        assert draft_row.status == "APPROVED"
        assert draft_row.published_article_id == body["article_id"]
        assert draft_row.reviewed_by == "admin1"
        assert draft_row.reviewed_at is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_draft_does_not_create_article(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    """Reject → draft.status='REJECTED', no Article row."""
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

    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{draft_id}/reject",
        params={"tenant_id": sample_tenant.id, "reviewer_id": "admin1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "REJECTED"

    # Verify NO Article was created.
    async with sm() as session:
        draft_row = (
            await session.execute(
                select(KbArticleDraft).where(KbArticleDraft.id == draft_id)
            )
        ).scalar_one()
        assert draft_row.status == "REJECTED"
        assert draft_row.published_article_id is None

        # No article for this tenant.
        articles = (
            await session.execute(
                select(Article).where(Article.tenant_id == sample_tenant.id)
            )
        ).scalars().all()
        assert articles == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_tenant_approve_returns_404(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    """Cross-tenant approve → 404 (anti-enumeration)."""
    sm = get_sessionmaker()
    draft_id = new_id()
    other_tenant_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    async with sm() as session:
        session.add(
            KbArticleDraft(
                id=draft_id,
                tenant_id=other_tenant_id,  # belongs to a DIFFERENT tenant
                cluster_id=0,
                source_questions=["q"],
                suggested_title="Foreign",
                suggested_body="b",
                suggested_tags=[],
                status="DRAFT",
            )
        )
        await session.commit()

    # sample_tenant tries to approve other_tenant's draft → 404
    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{draft_id}/approve",
        params={"tenant_id": sample_tenant.id, "reviewer_id": "admin1"},
    )
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_for_foreign_tenant_returns_empty(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    """A tenant listing its own drafts sees only its own (foreign = empty).

    This is the LIST-endpoint analog of the per-row 404 — there is
    no "drafts exist but you can't see them" branch.
    """
    sm = get_sessionmaker()
    other_tenant_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    async with sm() as session:
        session.add(
            KbArticleDraft(
                id=new_id(),
                tenant_id=other_tenant_id,
                cluster_id=0,
                source_questions=["q"],
                suggested_title="Foreign",
                suggested_body="b",
                suggested_tags=[],
                status="DRAFT",
            )
        )
        await session.commit()

    # Sample tenant lists — sees only its own (zero) drafts.
    resp = await async_client.get(
        "/api/v1/admin/kb-drafts",
        params={"tenant_id": sample_tenant.id},
    )
    assert resp.status_code == 200
    assert resp.json()["drafts"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approve_already_approved_returns_409(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    """Approve an already-APPROVED draft → 409 Conflict."""
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
                status="APPROVED",  # already approved
            )
        )
        await session.commit()

    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{draft_id}/approve",
        params={"tenant_id": sample_tenant.id, "reviewer_id": "admin1"},
    )
    assert resp.status_code == 409


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_already_rejected_returns_409(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    """Reject an already-REJECTED draft → 409 Conflict."""
    sm = get_sessionmaker()
    draft_id = new_id()
    async with sm() as session:
        session.add(
            KbArticleDraft(
                id=draft_id,
                tenant_id=sample_tenant.id,
                cluster_id=0,
                source_questions=["q"],
                suggested_title="T",
                suggested_body="b",
                suggested_tags=[],
                status="REJECTED",
            )
        )
        await session.commit()

    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{draft_id}/reject",
        params={"tenant_id": sample_tenant.id, "reviewer_id": "admin1"},
    )
    assert resp.status_code == 409