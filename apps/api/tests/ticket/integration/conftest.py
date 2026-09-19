"""Live-DB fixtures for the ticket REST API integration tests.

Mirrors the per-suite pattern from
``tests/knowledge/integration/test_api_crud.py`` and
``tests/agent/integration/conftest.py``:

* autouse singleton-reset (engine + sessionmaker) so each test gets a
  fresh event loop without ``RuntimeError: Event loop is closed``;
* tenant + channel + conversation factory that returns a fresh tuple
  on each call so tests can spin up multiple conversations;
* ``sample_ticket`` factory (returns a fully-persisted Ticket for the
  caller's tenant) — what the plan called ``sample_ticket`` /
  ``sample_ticket_with_events``;
* ``auth_headers`` helper for minting a real JWT.

PII discipline
--------------

Subjects are seeded with distinctive ``MAGIC_PHRASE_TICKET_*`` markers
so assertions never depend on real customer text. Logs are not
checked for PII here; the suite is only asserting HTTP status codes
and JSON shapes.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Tuple

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from auth.jwt import create_access_token
from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from channel.repository import ChannelRepository
from conversation.enums import ConversationStatus
from conversation.models import Conversation
from conversation.repository import ConversationRepository
from core.database import get_session, reset_engine, reset_sessionmaker
from core.id_gen import new_id
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository
from ticket.api import router as ticket_router
from ticket.enums import TicketStatus
from ticket.models import Ticket, TicketEvent
from ticket.repository import TicketRepository


# ---------------------------------------------------------------------------
# Singleton reset (autouse)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_db_singletons() -> None:
    """Reset the async engine / sessionmaker between tests.

    Each pytest-asyncio test runs in its own event loop. Without this
    fixture the engine from a previous test would try to reconnect on
    a closed loop and raise ``RuntimeError``.
    """
    reset_engine()
    reset_sessionmaker()
    yield
    reset_engine()
    reset_sessionmaker()


# ---------------------------------------------------------------------------
# App / client / auth
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Build a FastAPI app with just the ticket router mounted."""
    app = FastAPI()
    app.include_router(ticket_router)
    return app


@pytest.fixture
async def async_client() -> AsyncIterator[AsyncClient]:
    """Yield an httpx ``AsyncClient`` wired to the ticket-only FastAPI app."""
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


def _auth_headers(*, tenant_id: str, user_id: str = "u_admin") -> dict[str, str]:
    """Mint an admin JWT for ``tenant_id`` and return the auth header."""
    token = create_access_token(tenant_id=tenant_id, user_id=user_id, role="admin")
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Tenant / channel / conversation factories
# ---------------------------------------------------------------------------


async def _delete_tenant(tenant_id: str) -> None:
    """Cascade-delete a tenant (kills channels, conversations, messages, tickets)."""
    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t is not None:
            await session.delete(t)
            await session.commit()


async def _make_channel(*, tenant_id: str) -> Channel:
    """Insert an ACTIVE WEB channel for the tenant."""
    return await ChannelRepository().create(
        tenant_id=tenant_id,
        type=ChannelType.WEB,
        name="Ticket Test Channel",
        credentials_encrypted=json.dumps({}),
        status=ChannelStatus.ACTIVE,
    )


@pytest.fixture
async def tenant_factory() -> AsyncIterator[Callable[..., AsyncIterator[Tenant]]]:
    """Yield a factory that mints a fresh Tenant per call."""

    async def _factory(*, name: str = "Ticket Test Tenant") -> AsyncIterator[Tenant]:
        tenant = await TenantRepository().create(name=name, plan=TenantPlan.FREE)
        try:
            yield tenant
        finally:
            await _delete_tenant(tenant.id)

    yield _factory


@pytest.fixture
async def second_tenant_factory() -> AsyncIterator[
    Callable[..., AsyncIterator[Tenant]]
]:
    """Yield a factory for a SECOND tenant — cross-tenant isolation tests."""

    async def _factory(
        *, name: str = "Ticket Test Tenant B"
    ) -> AsyncIterator[Tenant]:
        tenant = await TenantRepository().create(name=name, plan=TenantPlan.FREE)
        try:
            yield tenant
        finally:
            await _delete_tenant(tenant.id)

    yield _factory


# ---------------------------------------------------------------------------
# Ticket fixture — the workhorse for the integration suite
# ---------------------------------------------------------------------------


@pytest.fixture
async def sample_ticket(
    tenant_factory: Callable[..., AsyncIterator[Tenant]],
) -> AsyncIterator[Ticket]:
    """Yield a fully-persisted NEW ticket for the caller's tenant.

    Sets up: tenant -> WEB channel -> OPEN conversation -> NEW ticket
    (subject starts with ``MAGIC_PHRASE_TICKET_001``). Cleanup is
    cascade-driven by the tenant delete in the inner fixture.
    """
    async for tenant in tenant_factory(
        name="Ticket Sample Tenant"
    ):
        channel = await _make_channel(tenant_id=tenant.id)
        now = datetime.now(UTC)
        conv = Conversation(
            id=new_id(),
            tenant_id=tenant.id,
            channel_id=channel.id,
            customer_external_id="ou_ticket_sample",
            status=ConversationStatus.OPEN,
            assigned_agent_id=None,
            ai_handling=True,
            opened_at=now,
            last_activity_at=now,
        )
        await ConversationRepository().create(conversation=conv)

        # Insert the Ticket directly via ORM (no API round-trip — this
        # fixture exists to seed the DB state the tests need).
        ticket = Ticket(
            id=new_id(),
            tenant_id=tenant.id,
            conversation_id=conv.id,
            subject="MAGIC_PHRASE_TICKET_001 sample subject",
            category=None,
            sla_deadline_at=now.replace(microsecond=0),
        )
        async with get_session() as session:
            session.add(ticket)
            await session.commit()
            await session.refresh(ticket)
        assert ticket.id is not None
        yield ticket


@pytest.fixture
async def sample_ticket_with_events(
    tenant_factory: Callable[..., AsyncIterator[Tenant]],
) -> AsyncIterator[Tuple[Ticket, list[TicketEvent]]]:
    """Yield a NEW ticket plus 3 audit events (created, commented, status_changed).

    Used by ``test_get_events_returns_ordered_desc`` — events are
    seeded with monotonically increasing ``created_at`` so DESC
    ordering is deterministic.
    """
    events: list[TicketEvent] = []
    async for tenant in tenant_factory(
        name="Ticket Sample Tenant w/ Events"
    ):
        channel = await _make_channel(tenant_id=tenant.id)
        now = datetime.now(UTC)
        conv = Conversation(
            id=new_id(),
            tenant_id=tenant.id,
            channel_id=channel.id,
            customer_external_id="ou_ticket_events",
            status=ConversationStatus.OPEN,
            assigned_agent_id=None,
            ai_handling=True,
            opened_at=now,
            last_activity_at=now,
        )
        await ConversationRepository().create(conversation=conv)

        ticket = Ticket(
            id=new_id(),
            tenant_id=tenant.id,
            conversation_id=conv.id,
            subject="MAGIC_PHRASE_TICKET_002 events subject",
            category=None,
            sla_deadline_at=now.replace(microsecond=0),
        )
        async with get_session() as session:
            session.add(ticket)
            await session.flush()
            base = now
            for i, evt_type in enumerate(
                ("created", "commented", "status_changed")
            ):
                ts = base.replace(microsecond=(base.microsecond + i) % 1_000_000)
                evt = TicketEvent(
                    id=new_id(),
                    tenant_id=tenant.id,
                    ticket_id=ticket.id,
                    actor_type="system" if i == 0 else "agent",
                    actor_id=None if i == 0 else "u_agent_1",
                    event_type=evt_type,
                    payload={"i": i},
                    created_at=ts,
                )
                session.add(evt)
                events.append(evt)
            await session.commit()
            for evt in events:
                await session.refresh(evt)
            await session.refresh(ticket)
        yield ticket, events


__all__ = [
    "sample_ticket",
    "sample_ticket_with_events",
    "async_client",
    "tenant_factory",
    "second_tenant_factory",
    "_auth_headers",
]