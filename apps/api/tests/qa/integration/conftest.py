"""Live-DB fixtures for the QA worker integration tests.

Stage 14 / Task 8 — wires up a real Postgres session so
``qa_judge_task`` and ``qa_sla_alert_worker`` can exercise the
production ORM path. The Judge client itself is mocked at the
``JudgeClient.score`` boundary — we never make a real LLM call
during integration tests.

Mirrors the per-suite pattern from
``tests/agent/integration/conftest.py`` (singleton reset + tenant
factory + message factory) so the suite stays hermetic and
cascades-cleanup-friendly.

PII discipline
--------------

Customer / AI messages carry distinctive ``MAGIC_PHRASE_QA_*``
markers so the worker logs and assertions never need real
customer text.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from channel.repository import ChannelRepository
from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation, Message
from conversation.repository import ConversationRepository, MessageRepository
from core.database import (
    get_session,
    get_sessionmaker,
    reset_engine,
    reset_sessionmaker,
)
from core.id_gen import new_id
from qa.models import MessageQaScore
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository


# ---------------------------------------------------------------------------
# Singleton reset (autouse)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_db_singletons() -> Any:
    """Reset the async engine / sessionmaker between tests."""
    reset_engine()
    reset_sessionmaker()
    yield
    reset_engine()
    reset_sessionmaker()


# ---------------------------------------------------------------------------
# Tenant / channel / conversation factories
# ---------------------------------------------------------------------------


async def _delete_tenant(tenant_id: str) -> None:
    """Cascade-delete a tenant (kills channels, conversations, messages)."""
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
        name="QA Test Channel",
        credentials_encrypted=json.dumps({}),
        status=ChannelStatus.ACTIVE,
    )


@pytest.fixture
async def tenant_factory() -> AsyncIterator[Callable[..., AsyncIterator[Tenant]]]:
    """Yield a factory that mints a fresh Tenant per call."""

    async def _factory(*, name: str = "QA Test Tenant") -> AsyncIterator[Tenant]:
        tenant = await TenantRepository().create(name=name, plan=TenantPlan.FREE)
        try:
            yield tenant
        finally:
            await _delete_tenant(tenant.id)

    yield _factory


# ---------------------------------------------------------------------------
# Conversation fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def sample_conversation(
    tenant_factory: Callable[..., AsyncIterator[Tenant]],
) -> AsyncIterator[tuple[Tenant, Channel, Conversation]]:
    """Yield ``(tenant, channel, conversation)`` for the worker's reads.

    Sets up: tenant → WEB channel → OPEN conversation. Cleanup
    cascades via the tenant delete in the inner fixture.
    """
    async for tenant in tenant_factory(name="QA Sample Conv Tenant"):
        channel = await _make_channel(tenant_id=tenant.id)
        now = datetime.now(UTC)
        conv = Conversation(
            id=new_id(),
            tenant_id=tenant.id,
            channel_id=channel.id,
            customer_external_id="ou_qa_sample",
            status=ConversationStatus.OPEN,
            assigned_agent_id=None,
            ai_handling=True,
            opened_at=now,
            last_activity_at=now,
        )
        await ConversationRepository().create(conversation=conv)
        yield tenant, channel, conv


# ---------------------------------------------------------------------------
# Message fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def sample_human_message(
    sample_conversation: tuple[Tenant, Channel, Conversation],
) -> AsyncIterator[Message]:
    """Yield a single CUSTOMER message in the sample conversation."""
    _tenant, _channel, conv = sample_conversation
    msg_repo = MessageRepository()
    msg = Message(
        id=new_id(),
        conversation_id=conv.id,
        role=MessageRole.CUSTOMER,
        content_text="MAGIC_PHRASE_QA_HUMAN_001 hello",
        sender_id=None,
        content_blocks_json=None,
        tool_calls_json=None,
        created_at=datetime.now(UTC),
    )
    await msg_repo.create(message=msg)
    yield msg


@pytest.fixture
async def sample_ai_message(
    sample_conversation: tuple[Tenant, Channel, Conversation],
    sample_human_message: Message,
) -> AsyncIterator[Message]:
    """Yield a single AI message in the sample conversation.

    Depends on ``sample_human_message`` so the
    ``get_last_customer_message`` lookup in the worker actually
    returns a row — this is the realistic shape the worker sees in
    production.
    """
    _tenant, _channel, conv = sample_conversation
    msg_repo = MessageRepository()
    # Bump the AI message's ``created_at`` so it's strictly newer
    # than the customer message — the worker's
    # ``get_last_customer_message`` correctly returns the customer
    # row even when the AI row exists.
    ts = datetime.now(UTC)
    msg = Message(
        id=new_id(),
        conversation_id=conv.id,
        role=MessageRole.AI,
        content_text="MAGIC_PHRASE_QA_AI_002 hi back",
        sender_id=None,
        content_blocks_json=None,
        tool_calls_json=None,
        created_at=ts,
    )
    await msg_repo.create(message=msg)
    yield msg


# ---------------------------------------------------------------------------
# Pre-seeded QA score fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def sample_existing_qa_score(
    sample_ai_message: Message,
    sample_conversation: tuple[Tenant, Channel, Conversation],
) -> AsyncIterator[MessageQaScore]:
    """Yield a ``MessageQaScore`` already persisted for ``sample_ai_message``.

    Used by the idempotency integration test — the second
    ``qa_judge_task`` call must short-circuit via
    ``exists_for_message``.
    """
    tenant, _channel, _conv = sample_conversation
    score = MessageQaScore(
        id=new_id(),
        tenant_id=tenant.id,
        message_id=sample_ai_message.id,
        judge_model="test-judge",
        relevance_score=0.5,
        safety_score=0.5,
        faithfulness_score=0.5,
        overall_score=0.5,
        rationale="MAGIC_PHRASE_QA_EXISTING_003",
        flagged=False,
    )
    async with get_session() as session:
        session.add(score)
        await session.commit()
        await session.refresh(score)
    yield score


# ---------------------------------------------------------------------------
# Session fixture (direct DB session for assertions)
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_session() -> AsyncIterator[Any]:
    """Yield an ``AsyncSession`` from the project sessionmaker for assertions.

    The session commits on exit (via ``get_session``'s default
    behaviour). Tests that need a long-lived session for read-only
    assertions should rely on a fresh session per assertion rather
    than reusing this one across multiple awaits.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        yield session