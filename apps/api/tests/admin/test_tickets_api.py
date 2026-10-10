"""Integration tests for the admin ticket list endpoint (Task 1.1).

Mirrors the M4.D budget endpoint test pattern: tenant-scoped, JWT
admin, 404 on cross-tenant, optional ``status``/``priority`` filters,
``limit`` + ``offset`` pagination, returns a ``TicketListRead``
envelope (``items`` + ``total``).

Why integration (httpx ``AsyncClient``) rather than mocked unit tests
(see the plan's speculative ``AsyncMock`` / ``patch`` sketch): the
M2.A bug-cluster autopsy (see memory file ``m2-a-bug-cluster.md``)
showed that in-memory unit tests on this module masked three
production-breaking bugs (``SAEnum.values_callable``, missing
``commit``, ``Depends(get_session)`` typing). The integration
fixtures use the real FastAPI app + a live ``AsyncSession`` so
SQLAlchemy <-> Postgres round-trips actually fire. ``SELECT *
FROM tickets WHERE ...`` against the real table is the load-bearing
test for the SAEnum lowercase fix.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from conversation.models import Conversation
from core.database import get_sessionmaker
from core.id_gen import new_id
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository
from tests.admin.conftest import auth_headers
from ticket.enums import TicketPriority, TicketStatus
from ticket.models import Ticket

# ---------------------------------------------------------------------------
# Tenant-table cleanup helper.
#
# The shared ``_delete_tenant`` in ``tests/admin/conftest.py`` uses
# ``session.delete(t)`` which relies on Postgres CASCADE on
# ``conversations.tenant_id`` → ``tickets.conversation_id``. That
# cascade chain works fine for clean rows in the same transaction,
# but when tickets accumulate across many failed tests in a session
# (because Postgres CASCADE on ``conversations.tenant_id`` fires
# against the ORIGINAL conversation row, which has since been
# modified) the constraint can fail on the much stricter
# ``tickets.tenant_id`` FK (which has NO ON DELETE action).
#
# We sidestep the whole cascade question by using
# ``TRUNCATE tenants CASCADE`` — Postgres itself walks the entire
# dependency graph and clears every dependent row, regardless of
# any per-FK ``ON DELETE`` clause declarations.
# ---------------------------------------------------------------------------


async def _safe_delete_tenant(tenant_id: str) -> None:
    """Idempotently wipe all rows for ``tenant_id`` and the tenant row itself."""
    sm = get_sessionmaker()
    async with sm() as session:
        # Reverse FK direction: delete child rows first (tickets,
        # ticket_events) so the tenant row can be removed cleanly.
        await session.execute(
            text("DELETE FROM ticket_events WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
        await session.execute(
            text("DELETE FROM tickets WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
        t = await session.get(Tenant, tenant_id)
        if t is not None:
            await session.delete(t)
            await session.commit()


@pytest.fixture(autouse=True)
async def _cleanup_sample_tenant_after_test(
    sample_tenant: Tenant, request: pytest.FixtureRequest
) -> AsyncIterator[None]:  # noqa: F821
    """After each test, robustly delete the sample tenant + its tickets.

    Replaces the default ``_delete_tenant`` cleanup path which can
    fail with FK violations when ticket rows persist (see
    ``_safe_delete_tenant`` docstring). Uses ``TRUNCATE CASCADE``
    on tenants to wipe everything regardless of FK direction.
    """
    yield
    # Teardown — only run when the sample_tenant fixture was used
    # in this test. ``request.fixturenames`` is set by pytest.
    if "sample_tenant" in request.fixturenames:
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(
                text("TRUNCATE TABLE tenants CASCADE")
            )
            await session.commit()

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_ticket(
    *,
    tenant_id: str,
    subject: str,
    status: TicketStatus = TicketStatus.NEW,
    priority: TicketPriority = TicketPriority.P2,
    created_at: datetime | None = None,
) -> Ticket:
    """Insert one ticket directly via ORM (no API round-trip).

    The conversation_id FK is non-nullable AND
    ``conversations.channel_id`` is FK-enforced to ``channels.id``,
    so we create a Channel + Conversation row before the Ticket.
    Mirrors the per-suite pattern in
    ``tests/ticket/integration/conftest.py::_make_channel``.
    """
    import json

    from channel.enums import ChannelStatus, ChannelType
    from channel.repository import ChannelRepository
    from conversation.enums import ConversationStatus
    from conversation.repository import ConversationRepository

    channel = await ChannelRepository().create(
        tenant_id=tenant_id,
        type=ChannelType.WEB,
        name=f"Tickets API Test Channel {subject}",
        credentials_encrypted=json.dumps({}),
        status=ChannelStatus.ACTIVE,
    )
    now = created_at or datetime.now(UTC)
    conv = Conversation(
        id=new_id(),
        tenant_id=tenant_id,
        channel_id=channel.id,
        customer_external_id=f"c-{new_id()}",
        status=ConversationStatus.OPEN,
        assigned_agent_id=None,
        ai_handling=False,
        opened_at=now,
        last_activity_at=now,
    )
    await ConversationRepository().create(conversation=conv)

    sm = get_sessionmaker()
    async with sm() as session:
        ticket = Ticket(
            id=new_id(),
            tenant_id=tenant_id,
            conversation_id=conv.id,
            subject=subject,
            category=None,
            priority=priority,
            status=status,
            created_at=now,
        )
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        return ticket


# ---------------------------------------------------------------------------
# Tenant factory (the sample_tenant fixture in conftest pins a single tenant;
# several tests below need two tenants for cross-tenant isolation).
# ---------------------------------------------------------------------------


async def _make_tenant(*, name: str) -> Tenant:
    return await TenantRepository().create(name=name, plan=TenantPlan.PRO)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_returns_tickets_for_tenant(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Happy path — listing returns the tenant's tickets + total count.

    Seeds two tickets and asserts the envelope contains both as
    ``items`` with ``total == 2``. Validates SAEnum round-trip
    (priority + status come back as the lowercase / "P0" strings,
    not the enum names).
    """
    a = await _seed_ticket(
        tenant_id=sample_tenant.id,
        subject="tickets-api-test-A",
        status=TicketStatus.NEW,
        priority=TicketPriority.P0,
    )
    await _seed_ticket(
        tenant_id=sample_tenant.id,
        subject="tickets-api-test-B",
        status=TicketStatus.TRIAGED,
        priority=TicketPriority.P1,
    )
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/tickets",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    # Both tickets must be present (any order — assertion below is by id).
    by_id = {item["id"]: item for item in body["items"]}
    assert a.id in by_id
    # SAEnum round-trip — priority is "P0" (not the enum name); status is
    # the lowercase PG-side value ("new"), not "NEW".
    assert by_id[a.id]["priority"] == "P0"
    assert by_id[a.id]["status"] == "new"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_filters_by_status(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Status query param narrows the result set."""
    for s, p in (
        (TicketStatus.NEW, TicketPriority.P0),
        (TicketStatus.RESOLVED, TicketPriority.P2),
        (TicketStatus.NEW, TicketPriority.P1),
    ):
        await _seed_ticket(
            tenant_id=sample_tenant.id,
            subject=f"tickets-api-status-{s.name}-{p.name}",
            status=s,
            priority=p,
        )
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/tickets",
        params={"status": "resolved"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Only the RESOLVED row matches.
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["status"] == "resolved"
    assert body["items"][0]["priority"] == "P2"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_filters_by_priority(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Priority query param narrows the result set."""
    await _seed_ticket(
        tenant_id=sample_tenant.id,
        subject="tickets-api-priority-p0",
        status=TicketStatus.NEW,
        priority=TicketPriority.P0,
    )
    await _seed_ticket(
        tenant_id=sample_tenant.id,
        subject="tickets-api-priority-p1",
        status=TicketStatus.NEW,
        priority=TicketPriority.P1,
    )
    await _seed_ticket(
        tenant_id=sample_tenant.id,
        subject="tickets-api-priority-p0-2",
        status=TicketStatus.TRIAGED,
        priority=TicketPriority.P0,
    )
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/tickets",
        params={"priority": "P0"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Two P0 rows (NEW + TRIAGED); the P1 row is excluded.
    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert all(item["priority"] == "P0" for item in body["items"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_pagination_limit_and_offset(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """limit + offset paginate the result set; total reflects the full filter.

    Seeds 4 tickets and asks for ``limit=2&offset=2`` — expect 2 items
    in the page, ``total == 4`` (the unpaginated count).
    """
    for i in range(4):
        await _seed_ticket(
            tenant_id=sample_tenant.id,
            subject=f"tickets-api-page-{i}",
            status=TicketStatus.NEW,
            priority=TicketPriority.P2,
        )
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/tickets",
        params={"limit": 2, "offset": 2},
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # total reflects the full result set, ignoring limit/offset.
    assert body["total"] == 4
    # Page 2 of 4 rows is exactly 2 items.
    assert len(body["items"]) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_cross_tenant_returns_404(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Anti-enumeration — admin of tenant A probing tenant B → 404.

    Seed a ticket in the SECOND tenant so the query at tenant B's
    path WOULD return data if the tenant gate were missing. Then
    assert the 404 from anti-enumeration.
    """
    tenant_b = await _make_tenant(name="Tickets API Cross Tenant B")
    try:
        await _seed_ticket(
            tenant_id=tenant_b.id,
            subject="tickets-api-cross-tenant",
            status=TicketStatus.NEW,
            priority=TicketPriority.P0,
        )
        # Admin token for sample_tenant, probing tenant_b.
        token = admin_token_for(tenant_id=sample_tenant.id)
        resp = await async_client.get(
            f"/api/v1/admin/tenants/{tenant_b.id}/tickets",
            headers=auth_headers(token),
        )
        assert resp.status_code == 404
        # Anti-enumeration: detail must not distinguish "exists but
        # wrong tenant" from "doesn't exist".
        assert "not found" in resp.json().get("detail", "").lower()
    finally:
        await _safe_delete_tenant(tenant_b.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_requires_admin_authentication(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    """No bearer token → 401; agent role → 403.

    The ``require_admin`` dep is the same gate used by every other
    endpoint in this module — exercise both branches so a future
    refactor that accidentally drops the gate would fail here.
    """
    # 401 — no token.
    no_auth = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/tickets"
    )
    assert no_auth.status_code == 401

    # 403 — agent role lacks the admin/owner gate.
    from auth.jwt import create_access_token

    agent_token = create_access_token(
        tenant_id=sample_tenant.id, user_id="agent-1", role="agent"
    )
    forbidden = await async_client.get(
        f"/api/v1/admin/tenants/{sample_tenant.id}/tickets",
        headers=auth_headers(agent_token),
    )
    assert forbidden.status_code == 403


__all__ = [
    "test_list_cross_tenant_returns_404",
    "test_list_filters_by_priority",
    "test_list_filters_by_status",
    "test_list_pagination_limit_and_offset",
    "test_list_requires_admin_authentication",
    "test_list_returns_tickets_for_tenant",
]
