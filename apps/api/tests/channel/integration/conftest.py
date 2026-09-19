"""Live-DB fixtures for the Stage 16 email inbound integration tests.

Mirrors the per-suite pattern from
``tests/ticket/integration/conftest.py`` (singleton reset, tenant +
channel + conversation factories) and adds:

* ``sample_tenant`` — a fully-seeded tenant + ACTIVE EMAIL channel
  whose ``config_json["address"]`` matches the support address the
  SES inbound tests use (``support@demo.test``);
* ``async_client`` — httpx AsyncClient wired to the FULL FastAPI app
  from ``main:app`` so the ``POST /api/v1/email/inbound`` route is
  registered alongside the rest of the API.
* ``db_session`` — yields the application sessionmaker (for tests that
  want to make assertions on persisted rows).

The test file imports ``from unittest.mock import MagicMock, AsyncMock``
inside the test bodies — those imports are NOT hoisted into the
fixtures so the conftest stays narrow.

PII discipline
--------------

All seed text carries a distinctive ``MAGIC_PHRASE_EMAIL_*`` marker
so assertions never depend on real customer text. The tests assert
against those markers only.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from channel.repository import ChannelRepository
from core.database import (
    get_sessionmaker,
    reset_engine,
    reset_sessionmaker,
)
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository


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
# App / client / session
# ---------------------------------------------------------------------------


@pytest.fixture
async def async_client() -> AsyncIterator[AsyncClient]:
    """Yield an httpx ``AsyncClient`` wired to the FULL FastAPI app.

    We import ``main`` here (rather than at module top) so the test
    collection step doesn't pay the cost of constructing the full
    application graph when running the rest of the suite (which uses
    per-suite sliced apps).
    """
    from main import app  # noqa: PLC0415 — import-after-fixture for fast collection

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
async def db_session() -> AsyncIterator[Any]:
    """Yield the application sessionmaker.

    Tests can use ``async with db_session() as session:`` to do
    per-row assertions on persisted messages / conversations.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        yield session


# ---------------------------------------------------------------------------
# Tenant / channel factories
# ---------------------------------------------------------------------------


async def _delete_tenant(tenant_id: str) -> None:
    """Cascade-delete a tenant (kills channels, conversations, messages)."""
    from core.database import get_session

    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t is not None:
            await session.delete(t)
            await session.commit()


async def _make_email_channel(
    *, tenant_id: str, address: str = "support@demo.test"
) -> Channel:
    """Insert an ACTIVE EMAIL channel for the tenant.

    The ``address`` is stored in ``config_json`` (the JSONB blob on the
    Channel model); the inbound webhook's recipient-routing query
    filters on this exact field via ``astext``.
    """
    return await ChannelRepository().create(
        tenant_id=tenant_id,
        type=ChannelType.EMAIL,
        name="Email Inbound Test Channel",
        credentials_encrypted=json.dumps({}),
        status=ChannelStatus.ACTIVE,
        config_json={"address": address},
    )


@pytest.fixture
async def sample_tenant() -> AsyncIterator[Tenant]:
    """Yield a tenant + matching ACTIVE EMAIL channel.

    Yields the ``Tenant`` row; the channel is reachable via the
    ``sample_email_channel`` fixture (added below) for tests that
    need the channel id directly.
    """
    tenant = await TenantRepository().create(
        name="Email Inbound Tenant", plan=TenantPlan.FREE
    )
    await _make_email_channel(tenant_id=tenant.id)
    try:
        yield tenant
    finally:
        await _delete_tenant(tenant.id)


@pytest.fixture
async def sample_email_channel(sample_tenant: Tenant) -> Channel:
    """Yield the EMAIL channel for ``sample_tenant``.

    Convenience accessor for tests that need the channel id. Created
    lazily against the freshly-minted tenant.
    """
    from core.database import get_session

    async with get_session() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(Channel)
            .where(
                Channel.tenant_id == sample_tenant.id,
                Channel.type == ChannelType.EMAIL,
            )
            .limit(1)
        )
        channel = result.scalar_one()
    return channel


__all__ = [
    "async_client",
    "db_session",
    "sample_tenant",
    "sample_email_channel",
]
