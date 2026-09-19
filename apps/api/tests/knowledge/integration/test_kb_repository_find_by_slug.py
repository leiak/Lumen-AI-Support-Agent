"""Live-DB integration tests for ``KnowledgeBaseRepository.find_by_slug``.

Stage 12 / Task 4 — closes the production-wiring gap flagged by the
final cross-stage review: ``search_internal_kb`` advertises
``kb_slug`` to the LLM but the repo had no ``find_by_slug`` method.
The tool guarded with ``hasattr(_kb_repo, "find_by_slug")`` and
silently dropped the slug filter in production.

These tests pin the new repo method's contract against the real
Postgres backend so a regression in the WHERE clause or the
``tenant_id`` filter surfaces immediately. Cross-tenant lookup
must return ``None`` (not 403, not exception) so the search tool
can downgrade to "all KBs" gracefully.

Cleanup is via cascading ``Tenant.delete``.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from core.database import get_session
from core.id_gen import new_id
from knowledge.models import KnowledgeBase
from knowledge.repository import KnowledgeBaseRepository
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository


# ============================================================================
# Fixtures / helpers
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_db_singletons() -> None:
    """Reset the async engine + sessionmaker between tests."""
    from core.database import reset_engine, reset_sessionmaker

    yield
    reset_engine()
    reset_sessionmaker()


async def _seed_tenant(*, name: str) -> Tenant:
    return await TenantRepository().create(name=name, plan=TenantPlan.FREE)


async def _delete_tenant(tenant_id: str) -> None:
    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t:
            await session.delete(t)
            await session.commit()


async def _count_kbs_for_tenant(tenant_id: str) -> int:
    async with get_session() as session:
        stmt = select(func.count()).select_from(KnowledgeBase).where(
            KnowledgeBase.tenant_id == tenant_id
        )
        return int((await session.execute(stmt)).scalar() or 0)


async def _seed_kb(
    *,
    tenant_id: str,
    slug: str,
    name: str = "KB",
) -> KnowledgeBase:
    """Create a KB row directly via SQL — bypasses the service layer
    because we want to pin the repo's read path, not the create path.
    """
    kb = KnowledgeBase(
        id=new_id(),
        tenant_id=tenant_id,
        name=name,
        slug=slug,
        description=None,
        embedding_model="text-embedding-3-small",
        chunk_size=800,
        chunk_overlap=100,
    )
    async with get_session() as session:
        session.add(kb)
        await session.commit()
        await session.refresh(kb)
    return kb


# ============================================================================
# Tests
# ============================================================================


@pytest.mark.integration
async def test_find_by_slug_returns_kb_for_tenant() -> None:
    """A KB seeded with slug ``"support"`` for tenant A is visible to
    ``find_by_slug`` for the same tenant.

    This is the happy-path the ``search_internal_kb`` tool relies on
    when the LLM passes ``kb_slug="support"``.
    """
    tenant = await _seed_tenant(name="Find By Slug Tenant A")
    seeded = await _seed_kb(tenant_id=tenant.id, slug="support", name="Support KB")
    try:
        repo = KnowledgeBaseRepository()
        found = await repo.find_by_slug(
            tenant_id=tenant.id, slug="support"
        )

        assert found is not None
        assert found.id == seeded.id
        assert found.tenant_id == tenant.id
        assert found.slug == "support"
        assert found.name == "Support KB"
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
async def test_find_by_slug_returns_none_for_wrong_tenant() -> None:
    """KB seeded under tenant A is INVISIBLE when tenant B queries it.

    Cross-tenant lookup returns ``None`` (same shape as "missing") —
    no exception, no leak. This is the M1 tenant-isolation
    contract at the repo layer.
    """
    tenant_a = await _seed_tenant(name="Find By Slug Tenant A2")
    tenant_b = await _seed_tenant(name="Find By Slug Tenant B2")
    await _seed_kb(tenant_id=tenant_a.id, slug="support", name="Support A")
    try:
        repo = KnowledgeBaseRepository()
        # Tenant B asks for tenant A's slug — must NOT see it.
        found = await repo.find_by_slug(
            tenant_id=tenant_b.id, slug="support"
        )
        assert found is None, (
            "cross-tenant slug lookup leaked: tenant B saw tenant A's KB"
        )
        # And tenant A still can see its own row.
        found_a = await repo.find_by_slug(
            tenant_id=tenant_a.id, slug="support"
        )
        assert found_a is not None
        assert found_a.tenant_id == tenant_a.id
    finally:
        await _delete_tenant(tenant_a.id)
        await _delete_tenant(tenant_b.id)


@pytest.mark.integration
async def test_find_by_slug_returns_none_for_missing_slug() -> None:
    """Asking for a slug that doesn't exist returns ``None`` (not 404,
    not exception). The search tool relies on this so it can fall back
    to "search all tenant KBs" without crashing the LLM turn.
    """
    tenant = await _seed_tenant(name="Find By Slug Missing")
    await _seed_kb(tenant_id=tenant.id, slug="billing", name="Billing KB")
    try:
        repo = KnowledgeBaseRepository()
        found = await repo.find_by_slug(
            tenant_id=tenant.id, slug="nonexistent-slug"
        )
        assert found is None
    finally:
        await _delete_tenant(tenant.id)


@pytest.mark.integration
async def test_find_by_slug_returns_none_when_tenant_has_no_kbs() -> None:
    """A tenant with zero KBs gets ``None`` for any slug query.

    Defensive path — the search tool needs this so a brand-new
    tenant's "search_internal_kb" call doesn't 500.
    """
    tenant = await _seed_tenant(name="Find By Slug Empty Tenant")
    try:
        repo = KnowledgeBaseRepository()
        # Sanity: zero KBs in the table for this tenant.
        assert await _count_kbs_for_tenant(tenant.id) == 0
        found = await repo.find_by_slug(tenant_id=tenant.id, slug="anything")
        assert found is None
    finally:
        await _delete_tenant(tenant.id)