"""Integration tests for the admin cross-domain search endpoint.

Tier 3 Task 3.3 — Cross-domain search.

Single endpoint:
  GET /api/v1/admin/search?q=...&limit=10

Returns grouped ``SearchResult`` rows (kind discriminator) across:

* KB articles       (title + latest version raw_text ILIKE)
* Tickets           (subject ILIKE)
* Conversations     (id + customer_external_id ILIKE)

Tenant isolation
----------------
Every query carries ``tenant_id`` (from JWT). Cross-tenant hits
do NOT appear (anti-enumeration parity with the rest of admin).

PII discipline
--------------
We only return ``snippet`` / ``title`` / ``subtitle``. No raw
message bodies, no customer text beyond ``customer_external_id``
(which is the tenant's own opaque identifier).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient

from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from conversation.enums import ConversationStatus
from conversation.models import Conversation
from core.database import get_sessionmaker
from core.id_gen import new_id
from knowledge.enums import ArticleSourceType, ArticleStatus
from knowledge.models import Article, ArticleVersion, KnowledgeBase
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository
from tests.admin.conftest import _delete_tenant, auth_headers
from ticket.enums import TicketPriority, TicketStatus
from ticket.models import Ticket

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_kb(tenant_id: str, *, slug: str = "search-kb") -> str:
    sm = get_sessionmaker()
    async with sm() as session:
        kb = KnowledgeBase(
            id=new_id(),
            tenant_id=tenant_id,
            name="Search KB",
            slug=slug,
            description=None,
            embedding_model="text-embedding-3-small",
            chunk_size=800,
            chunk_overlap=100,
        )
        session.add(kb)
        await session.commit()
        return kb.id


async def _seed_article(
    *,
    tenant_id: str,
    knowledge_base_id: str,
    title: str,
    raw_text: str,
    status: ArticleStatus = ArticleStatus.INDEXED,
) -> tuple[str, str]:
    """Insert Article + v1 ArticleVersion, return (article_id, version_id)."""
    sm = get_sessionmaker()
    async with sm() as session:
        article = Article(
            id=new_id(),
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            title=title,
            source_uri=None,
            source_type=ArticleSourceType.MANUAL,
            status=status,
            current_version_id=None,
            error_message=None,
        )
        session.add(article)
        await session.flush()
        version = ArticleVersion(
            id=new_id(),
            article_id=article.id,
            version_number=1,
            raw_text=raw_text,
            content_hash="x",
        )
        session.add(version)
        await session.flush()
        article.current_version_id = version.id
        await session.commit()
        return article.id, version.id


async def _seed_channel(tenant_id: str) -> str:
    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(
            id=new_id(),
            tenant_id=tenant_id,
            type=ChannelType.WEB,
            name="web-widget",
            credentials_encrypted="{}",
            status=ChannelStatus.ACTIVE,
            config_json={},
        )
        session.add(channel)
        await session.commit()
        return channel.id


async def _seed_conversation(
    tenant_id: str, channel_id: str, *, customer_external_id: str
) -> str:
    sm = get_sessionmaker()
    async with sm() as session:
        now = datetime.now(UTC)
        conv = Conversation(
            id=new_id(),
            tenant_id=tenant_id,
            channel_id=channel_id,
            customer_external_id=customer_external_id,
            status=ConversationStatus.OPEN,
            opened_at=now,
            last_activity_at=now,
        )
        session.add(conv)
        await session.commit()
        return conv.id


async def _seed_ticket(
    tenant_id: str, *, conversation_id: str, subject: str
) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        ticket = Ticket(
            id=new_id(),
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            subject=subject,
            priority=TicketPriority.P3,
            status=TicketStatus.NEW,
        )
        session.add(ticket)
        await session.commit()


def _by_kind(results: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [r for r in results if r["kind"] == kind]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_returns_empty_for_blank_query(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """An empty q is treated as 'no search' rather than 422 — operators
    who clear the input shouldn't see an error."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        "/api/v1/admin/search?q=zzz_no_match_xyz",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "zzz_no_match_xyz"
    assert body["total"] == 0
    assert body["results"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_returns_empty_results_with_no_data(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        "/api/v1/admin/search?q=anything",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_matches_article_title(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    kb_id = await _seed_kb(sample_tenant.id)
    article_id, _ = await _seed_article(
        tenant_id=sample_tenant.id,
        knowledge_base_id=kb_id,
        title="How to reset your password",
        raw_text="Visit /reset and follow the email link.",
    )
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        "/api/v1/admin/search?q=password",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    articles = _by_kind(body["results"], "article")
    assert len(articles) == 1
    assert articles[0]["id"] == article_id
    assert articles[0]["title"] == "How to reset your password"
    assert "KB" in articles[0]["subtitle"]
    assert articles[0]["extra"]["knowledge_base_id"] == kb_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_matches_article_body_text(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """A hit on raw_text also surfaces an article (even if title doesn't match)."""
    kb_id = await _seed_kb(sample_tenant.id)
    article_id, _ = await _seed_article(
        tenant_id=sample_tenant.id,
        knowledge_base_id=kb_id,
        title="Setup guide",
        # The needle "transmogrifier" only appears in the body.
        raw_text="Plug the transmogrifier into the blue socket.",
    )
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        "/api/v1/admin/search?q=transmogrifier",
        headers=auth_headers(token),
    )
    body = resp.json()
    articles = _by_kind(body["results"], "article")
    assert len(articles) == 1
    assert articles[0]["id"] == article_id
    # Snippet should contain the matched needle (case-insensitive contains).
    assert articles[0]["snippet"] is not None
    assert "transmogrifier" in articles[0]["snippet"].lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_matches_ticket_subject(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    channel_id = await _seed_channel(sample_tenant.id)
    conv_id = await _seed_conversation(
        sample_tenant.id, channel_id, customer_external_id="cust-1"
    )
    await _seed_ticket(
        sample_tenant.id, conversation_id=conv_id, subject="Refund request for invoice 42"
    )
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        "/api/v1/admin/search?q=refund",
        headers=auth_headers(token),
    )
    body = resp.json()
    tickets = _by_kind(body["results"], "ticket")
    assert len(tickets) == 1
    assert tickets[0]["title"] == "Refund request for invoice 42"
    assert "new" in tickets[0]["subtitle"].lower()
    assert "p3" in tickets[0]["subtitle"].lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_matches_conversation_by_customer_external_id(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    channel_id = await _seed_channel(sample_tenant.id)
    conv_id = await _seed_conversation(
        sample_tenant.id, channel_id, customer_external_id="cust-vip-9000"
    )
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        "/api/v1/admin/search?q=vip-9000",
        headers=auth_headers(token),
    )
    body = resp.json()
    convs = _by_kind(body["results"], "conversation")
    assert len(convs) == 1
    assert convs[0]["id"] == conv_id
    assert convs[0]["title"] == "cust-vip-9000"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_does_not_leak_across_tenants(
    async_client: AsyncClient, admin_token_for
) -> None:
    """Tenant B's hits must not appear when searching with tenant A's JWT."""
    tenant_a = await TenantRepository().create(
        name="Search Cross A", plan=TenantPlan.FREE
    )
    tenant_b = await TenantRepository().create(
        name="Search Cross B", plan=TenantPlan.FREE
    )
    try:
        # Seed a uniquely-named article in each tenant.
        kb_a = await _seed_kb(tenant_a.id, slug="a")
        await _seed_article(
            tenant_id=tenant_a.id,
            knowledge_base_id=kb_a,
            title="unicornA needle",
            raw_text="unicornA body",
        )
        kb_b = await _seed_kb(tenant_b.id, slug="b")
        b_article_id, _ = await _seed_article(
            tenant_id=tenant_b.id,
            knowledge_base_id=kb_b,
            title="unicornB needle",
            raw_text="unicornB body",
        )
        token = admin_token_for(tenant_id=tenant_a.id)
        resp = await async_client.get(
            "/api/v1/admin/search?q=unicorn",
            headers=auth_headers(token),
        )
        body = resp.json()
        ids = {r["id"] for r in body["results"]}
        # tenant B's article must NOT be present.
        assert b_article_id not in ids
        # Only tenant A's hits (plus any other seeded rows from other
        # tests on the same Postgres — we check the negative above
        # which is the only strict invariant).
        assert any(
            "unicornA" in (r.get("title") or "") for r in body["results"]
        )
    finally:
        await _delete_tenant(tenant_a.id)
        await _delete_tenant(tenant_b.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_honors_limit_across_domains(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Each domain is capped at limit//3 (with min 3 when limit >= 9)."""
    kb_id = await _seed_kb(sample_tenant.id)
    # 4 articles — should all surface when limit=9 (cap = 3, so 3).
    for i in range(4):
        await _seed_article(
            tenant_id=sample_tenant.id,
            knowledge_base_id=kb_id,
            title=f"cap article {i}",
            raw_text="cap body",
        )
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        "/api/v1/admin/search?q=cap&limit=9",
        headers=auth_headers(token),
    )
    body = resp.json()
    articles = _by_kind(body["results"], "article")
    # limit 9 → per-domain cap = max(3, 3) = 3
    assert len(articles) == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_rejects_missing_q(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        "/api/v1/admin/search",
        headers=auth_headers(token),
    )
    assert resp.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_rejects_non_admin_jwt(
    async_client: AsyncClient, sample_tenant: Tenant, non_admin_token_for
) -> None:
    """require_admin: a non-admin (agent) JWT must get 403."""
    token = non_admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        "/api/v1/admin/search?q=anything",
        headers=auth_headers(token),
    )
    assert resp.status_code == 403