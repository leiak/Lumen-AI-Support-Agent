"""Integration tests for the admin Dashboard aggregate endpoint.

Tier 2 Task 2.5 — Admin Dashboard.

Single endpoint:
  GET /api/v1/admin/tenants/{tenant_id}/dashboard

Returns KPI counts scoped to the caller's tenant:

* ``tickets_today`` (since UTC midnight) + ``tickets_by_status`` +
  ``tickets_by_priority`` (group-by buckets, not just totals — the
  SPA renders small charts off the same payload)
* ``conversations_open`` (status in {open, pending})
* ``llm_tokens_this_period`` (current UTC month, prompt+completion)
* ``active_users`` / ``total_users`` (is_active flag breakdown)

Multi-tenant isolation
----------------------
Cross-tenant access returns 404 (anti-enumeration, mirrors the rest
of the admin surface). Non-admin role gets 403.

PII discipline
--------------
Dashboard payload is *counts only* — no PII, no message bodies,
no customer text. Tests use opaque ULIDs / fake customer IDs.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from conversation.enums import ConversationStatus
from conversation.models import Conversation
from core.database import get_sessionmaker
from core.id_gen import new_id
from llm_client.models import LLMUsage
from tenant.enums import TenantPlan, UserRole
from tenant.models import Tenant, User
from tests.admin.conftest import auth_headers
from ticket.enums import TicketPriority, TicketStatus
from ticket.models import Ticket


# ---------------------------------------------------------------------------
# Seed helpers — each test seeds only what it needs.
# ---------------------------------------------------------------------------


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
    tenant_id: str, channel_id: str, *, status: ConversationStatus
) -> str:
    sm = get_sessionmaker()
    async with sm() as session:
        now = datetime.now(UTC)
        conv = Conversation(
            id=new_id(),
            tenant_id=tenant_id,
            channel_id=channel_id,
            customer_external_id=f"cust-{new_id()}",
            status=status,
            opened_at=now,
            last_activity_at=now,
        )
        session.add(conv)
        await session.commit()
        return conv.id


async def _seed_ticket(
    tenant_id: str,
    *,
    conversation_id: str,
    status: TicketStatus = TicketStatus.NEW,
    priority: TicketPriority = TicketPriority.P2,
) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        ticket = Ticket(
            id=new_id(),
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            subject="dashboard seed ticket",
            priority=priority,
            status=status,
        )
        session.add(ticket)
        await session.commit()


async def _seed_user(
    tenant_id: str,
    *,
    email: str | None = None,
    is_active: bool = True,
    role: UserRole = UserRole.AGENT,
) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        user = User(
            id=new_id(),
            tenant_id=tenant_id,
            email=email or f"u-{new_id()}@example.com",
            password_hash="x",
            role=role,
            is_active=is_active,
            full_name="Seed",
        )
        session.add(user)
        await session.commit()


async def _seed_llm_usage(
    tenant_id: str, *, prompt_tokens: int, completion_tokens: int
) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        row = LLMUsage(
            id=new_id(),
            tenant_id=tenant_id,
            provider="openai",
            model="gpt-4o-mini",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            request_id=new_id(),
        )
        session.add(row)
        await session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dashboard_returns_zero_counts_when_tenant_is_empty(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Fresh tenant — every count is zero, breakdowns are empty."""
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/dashboard",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["tickets_today"] == 0
    assert body["tickets_by_status"] == []
    assert body["tickets_by_priority"] == []
    assert body["conversations_open"] == 0
    assert body["llm_tokens_this_period"] == 0
    assert body["active_users"] == 0
    assert body["total_users"] == 0
    # Period anchor.
    assert isinstance(body["period"], str)
    assert len(body["period"]) == 7  # YYYY-MM
    assert "T" in body["today_utc_start"]
    assert "T" in body["period_starts_at"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dashboard_counts_tickets_by_status_and_priority(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Tickets are bucketed correctly; only P0+P1 priority surface."""
    channel_id = await _seed_channel(sample_tenant.id)
    # 2 NEW tickets at P2, 1 IN_PROGRESS at P0, 1 RESOLVED at P3.
    for status, prio in [
        (TicketStatus.NEW, TicketPriority.P2),
        (TicketStatus.NEW, TicketPriority.P2),
        (TicketStatus.IN_PROGRESS, TicketPriority.P0),
        (TicketStatus.RESOLVED, TicketPriority.P3),
    ]:
        cid = await _seed_conversation(
            sample_tenant.id, channel_id, status=ConversationStatus.OPEN
        )
        await _seed_ticket(
            sample_tenant.id,
            conversation_id=cid,
            status=status,
            priority=prio,
        )

    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/dashboard",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["tickets_today"] == 4

    by_status = {row["key"]: row["count"] for row in body["tickets_by_status"]}
    assert by_status == {"new": 2, "in_progress": 1, "resolved": 1}

    by_priority = {
        row["key"]: row["count"] for row in body["tickets_by_priority"]
    }
    assert by_priority == {"P0": 1, "P2": 2, "P3": 1}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dashboard_open_conversations_include_pending(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """``conversations_open`` covers both OPEN and PENDING; CLOSED excluded."""
    channel_id = await _seed_channel(sample_tenant.id)
    for status in [
        ConversationStatus.OPEN,
        ConversationStatus.OPEN,
        ConversationStatus.PENDING,
        ConversationStatus.CLOSED,
        ConversationStatus.CLOSED,
    ]:
        await _seed_conversation(
            sample_tenant.id, channel_id, status=status
        )

    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/dashboard",
        headers=auth_headers(token),
    )
    body = resp.json()
    assert body["conversations_open"] == 3  # 2 OPEN + 1 PENDING


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dashboard_sums_llm_tokens_this_period(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """``llm_tokens_this_period`` = sum(prompt_tokens + completion_tokens)."""
    await _seed_llm_usage(
        sample_tenant.id, prompt_tokens=100, completion_tokens=50
    )
    await _seed_llm_usage(
        sample_tenant.id, prompt_tokens=200, completion_tokens=25
    )
    await _seed_llm_usage(
        sample_tenant.id, prompt_tokens=0, completion_tokens=10
    )

    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/dashboard",
        headers=auth_headers(token),
    )
    body = resp.json()
    assert body["llm_tokens_this_period"] == 385  # 150 + 225 + 10


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dashboard_counts_active_and_total_users(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """``active_users`` < ``total_users`` when an inactive user exists."""
    for active in (True, True, True, False):
        await _seed_user(sample_tenant.id, is_active=active)

    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/dashboard",
        headers=auth_headers(token),
    )
    body = resp.json()
    assert body["active_users"] == 3
    assert body["total_users"] == 4


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dashboard_cross_tenant_access_returns_404(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Admin of another tenant gets 404 (anti-enumeration)."""
    other_tenant_id = "01HZOTHER00000000000000000"
    token = admin_token_for(tenant_id=other_tenant_id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/dashboard",
        headers=auth_headers(token),
    )
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dashboard_non_admin_role_returns_403(
    async_client: AsyncClient, sample_tenant: Tenant, non_admin_token_for
) -> None:
    """Agent role lacks admin privileges → 403."""
    token = non_admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/dashboard",
        headers=auth_headers(token),
    )
    assert resp.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dashboard_no_token_returns_401(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    """Missing bearer token → 401."""
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/dashboard"
    )
    assert resp.status_code == 401


__all__ = [
    "test_dashboard_returns_zero_counts_when_tenant_is_empty",
    "test_dashboard_counts_tickets_by_status_and_priority",
    "test_dashboard_open_conversations_include_pending",
    "test_dashboard_sums_llm_tokens_this_period",
    "test_dashboard_counts_active_and_total_users",
    "test_dashboard_cross_tenant_access_returns_404",
    "test_dashboard_non_admin_role_returns_403",
    "test_dashboard_no_token_returns_401",
]