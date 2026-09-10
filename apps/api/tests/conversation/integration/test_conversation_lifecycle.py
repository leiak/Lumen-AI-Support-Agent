"""Live-DB integration tests for the conversation domain.

Verifies the conversation + message lifecycle against a real Postgres
database, exercising:

- the partial unique index on (channel_id, customer_external_id) that
  prevents duplicate OPEN conversations for the same (channel, customer);
- the race-resolve path on concurrent inbound envelopes;
- the atomic write of a Message + last_activity_at;
- the OPEN -> PENDING -> CLOSED state machine;
- cross-tenant access returning 404 (anti-enumeration) through the REST API;
- the widget WS receiving a ``message.complete`` frame that carries the AI
  auto-reply (separate from the ack).

These tests intentionally do NOT mock ``ConversationService`` or its
repositories. The point is to exercise the real DB constraints and
service code paths end-to-end. Cleanup is via cascading Tenant delete in
a ``finally:`` block.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from auth.jwt import create_access_token
from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from channel.repository import ChannelRepository
from conversation.api import router as conversations_router
from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation
from conversation.repository import ConversationRepository, MessageRepository
from conversation.service import ConversationService
from core.database import get_session
from core.id_gen import new_id
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository
from widget.api import router as widget_api_router
from widget.tokens import create_widget_token
from widget.ws.manager import ConnectionManager
from widget.ws.router import router as widget_ws_router

# ============================================================================
# Fixtures / helpers
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_db_singletons():
    """Reset async engine/sessionmaker between tests to avoid cross-loop contamination."""
    from core.database import reset_engine, reset_sessionmaker

    yield
    reset_engine()
    reset_sessionmaker()


async def _seed_tenant_and_web_channel(
    *, name: str = "Lifecycle Tenant"
) -> tuple[Tenant, Channel]:
    """Seed a tenant + an ACTIVE WEB channel. Returns (tenant, channel).

    Cleanup is the test's responsibility via ``_delete_tenant`` in a finally.
    """
    tenant = await TenantRepository().create(name=name, plan=TenantPlan.FREE)
    channel = await ChannelRepository().create(
        tenant_id=tenant.id,
        type=ChannelType.WEB,
        name=f"{name} Web Channel",
        credentials_encrypted=json.dumps({}),
        status=ChannelStatus.ACTIVE,
    )
    return tenant, channel


async def _delete_tenant(tenant_id: str) -> None:
    """Cascade-delete a tenant (kills its channels, conversations, messages)."""
    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t:
            await session.delete(t)
            await session.commit()


# ============================================================================
# Test 1 — repository round-trip + partial unique index interplay
# ============================================================================


@pytest.mark.integration
async def test_repo_round_trip_with_partial_unique_index() -> None:
    """``create`` + ``find_open_by_channel_customer`` round-trip, plus the
    partial unique index that allows a new OPEN conversation after the
    previous one is CLOSED.

    Steps:
      1. Seed tenant + channel.
      2. ``ConversationRepository.create`` a new Conversation.
      3. ``find_open_by_channel_customer`` returns it.
      4. ``update`` to CLOSED.
      5. ``find_open_by_channel_customer`` returns None.
      6. ``create`` a SECOND conversation for the same (channel, customer)
         — must succeed because the partial unique index only covers
         non-CLOSED rows.
    """
    tenant, channel = await _seed_tenant_and_web_channel()
    try:
        repo = ConversationRepository()
        now = datetime.now(UTC)
        first = Conversation(
            id=new_id(),
            tenant_id=tenant.id,
            channel_id=channel.id,
            customer_external_id="ou_roundtrip",
            status=ConversationStatus.OPEN,
            assigned_agent_id=None,
            ai_handling=True,
            opened_at=now,
            last_activity_at=now,
        )
        first = await repo.create(conversation=first)
        assert first.id is not None

        # 3. find_open returns it
        found = await repo.find_open_by_channel_customer(
            channel_id=channel.id, customer_external_id="ou_roundtrip"
        )
        assert found is not None
        assert found.id == first.id

        # 4. close it
        first.status = ConversationStatus.CLOSED
        first.ai_handling = False
        closed = await repo.update(first)
        assert closed is not None
        assert closed.status == ConversationStatus.CLOSED

        # 5. find_open no longer returns it
        found_after_close = await repo.find_open_by_channel_customer(
            channel_id=channel.id, customer_external_id="ou_roundtrip"
        )
        assert found_after_close is None

        # 6. creating a SECOND OPEN conversation for the same (channel,
        # customer) must succeed — the partial unique index excludes
        # CLOSED rows.
        second = Conversation(
            id=new_id(),
            tenant_id=tenant.id,
            channel_id=channel.id,
            customer_external_id="ou_roundtrip",
            status=ConversationStatus.OPEN,
            assigned_agent_id=None,
            ai_handling=True,
            opened_at=now,
            last_activity_at=now,
        )
        second = await repo.create(conversation=second)
        assert second.id is not None
        assert second.id != first.id

        # And the new one is now visible to find_open
        found_again = await repo.find_open_by_channel_customer(
            channel_id=channel.id, customer_external_id="ou_roundtrip"
        )
        assert found_again is not None
        assert found_again.id == second.id
        assert found_again.status == ConversationStatus.OPEN
    finally:
        await _delete_tenant(tenant.id)


# ============================================================================
# Test 2 — race-resolve against the real partial unique index
# ============================================================================


@pytest.mark.integration
async def test_find_or_create_for_inbound_resolves_race() -> None:
    """Two concurrent ``find_or_create_for_inbound`` calls for the same
    (channel, customer) must produce exactly ONE Conversation row.

    Uses ``asyncio.gather`` with a barrier to synchronize the start of
    both calls. The post-condition is checked against the live DB: there
    must be exactly one OPEN conversation for the pair, and both callers
    must have received the same conversation id.
    """
    tenant, channel = await _seed_tenant_and_web_channel()
    try:
        service = ConversationService()
        # ``asyncio.Barrier`` waits for *both* parties to reach the barrier
        # before releasing them at the same instant. Combined with gather,
        # this maximises the chance both find_or_create calls enter their
        # read-then-create window before either commits.
        barrier = asyncio.Barrier(2)

        async def open_one() -> Conversation | None:
            await barrier.wait()
            return await service.find_or_create_for_inbound(
                tenant_id=tenant.id,
                channel_id=channel.id,
                customer_external_id="ou_race",
            )

        results = await asyncio.gather(open_one(), open_one())

        # Both callers received a Conversation
        assert results[0] is not None
        assert results[1] is not None
        # And they received the SAME conversation
        assert results[0].id == results[1].id

        # Exactly ONE OPEN conversation in the DB for this pair
        repo = ConversationRepository()
        opened = await repo.find_open_by_channel_customer(
            channel_id=channel.id, customer_external_id="ou_race"
        )
        assert opened is not None
        assert opened.id == results[0].id

        # Sanity: count via list_by_tenant should be 1 for this customer pair
        all_for_tenant = await repo.list_by_tenant(tenant_id=tenant.id)
        matches = [
            c
            for c in all_for_tenant
            if c.channel_id == channel.id
            and c.customer_external_id == "ou_race"
            and c.status == ConversationStatus.OPEN
        ]
        assert len(matches) == 1, f"expected exactly 1 OPEN row, got {len(matches)}"
    finally:
        await _delete_tenant(tenant.id)


# ============================================================================
# Test 3 — record_message writes Message + last_activity_at atomically
# ============================================================================


@pytest.mark.integration
async def test_record_message_atomic_timestamp() -> None:
    """``record_message`` must write the Message AND advance
    ``last_activity_at`` to the SAME timestamp.

    Verified against the live DB by reading both rows back and asserting
    the timestamps are equal.
    """
    tenant, channel = await _seed_tenant_and_web_channel()
    try:
        service = ConversationService()
        # Create a fresh conversation via the service to set baseline
        opened = await service.find_or_create_for_inbound(
            tenant_id=tenant.id,
            channel_id=channel.id,
            customer_external_id="ou_ts",
        )
        assert opened is not None
        baseline_ts = opened.last_activity_at

        # Wait a microsecond so the timestamp we record is strictly after baseline
        await asyncio.sleep(0.01)

        msg = await service.record_message(
            tenant_id=tenant.id,
            conversation_id=opened.id,
            role=MessageRole.CUSTOMER,
            content_text="hi with timestamp",
            sender_id="u_cust_ts",
        )

        # Re-fetch from DB to bypass any in-memory cache
        conv_repo = ConversationRepository()
        msg_repo = MessageRepository()

        conv = await conv_repo.get_by_id(opened.id)
        assert conv is not None
        msgs = await msg_repo.list_by_conversation(conversation_id=opened.id)

        assert len(msgs) == 1
        recorded = msgs[0]
        assert recorded.id == msg.id
        assert recorded.role == MessageRole.CUSTOMER
        assert recorded.content_text == "hi with timestamp"
        assert recorded.sender_id == "u_cust_ts"

        # Atomicity check: the message created_at and the conversation's
        # last_activity_at must be equal (same wall-clock instant).
        assert recorded.created_at == conv.last_activity_at
        # And strictly newer than the baseline
        assert conv.last_activity_at > baseline_ts
    finally:
        await _delete_tenant(tenant.id)


# ============================================================================
# Test 4 — full lifecycle
# ============================================================================


@pytest.mark.integration
async def test_full_conversation_lifecycle_via_service() -> None:
    """End-to-end lifecycle through ``ConversationService`` only:

      1. find_or_create_for_inbound -> OPEN, ai_handling=True
      2. record_message(CUSTOMER) -> 1 message
      3. record_message(AI) -> 2 messages
      4. assign_to_agent -> PENDING, ai_handling=False
      5. record_message(AGENT) -> 3 messages, still PENDING
      6. return_to_ai -> OPEN, ai_handling=True, assigned_agent_id=None
      7. close -> CLOSED, ai_handling=False
      8. After close, find_or_create_for_inbound creates a NEW conversation
         for the same (channel, customer); the closed one stays CLOSED.
    """
    tenant, channel = await _seed_tenant_and_web_channel()
    try:
        service = ConversationService()
        conv_repo = ConversationRepository()
        msg_repo = MessageRepository()

        # 1
        conv = await service.find_or_create_for_inbound(
            tenant_id=tenant.id,
            channel_id=channel.id,
            customer_external_id="ou_lifecycle",
        )
        assert conv is not None
        assert conv.status == ConversationStatus.OPEN
        assert conv.ai_handling is True
        assert conv.assigned_agent_id is None

        # 2
        await service.record_message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            role=MessageRole.CUSTOMER,
            content_text="hi",
        )
        msgs = await msg_repo.list_by_conversation(conversation_id=conv.id)
        assert len(msgs) == 1
        assert msgs[0].role == MessageRole.CUSTOMER
        assert msgs[0].content_text == "hi"

        # 3
        await service.record_message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            role=MessageRole.AI,
            content_text="hello there",
        )
        msgs = await msg_repo.list_by_conversation(conversation_id=conv.id)
        assert len(msgs) == 2
        roles = [m.role for m in msgs]
        assert MessageRole.CUSTOMER in roles
        assert MessageRole.AI in roles

        # 4
        agent_id = new_id()
        updated = await service.assign_to_agent(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            agent_id=agent_id,
        )
        assert updated is not None
        assert updated.status == ConversationStatus.PENDING
        assert updated.ai_handling is False
        assert updated.assigned_agent_id == agent_id

        # 5
        await service.record_message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            role=MessageRole.AGENT,
            content_text="human reply",
            sender_id=agent_id,
        )
        msgs = await msg_repo.list_by_conversation(conversation_id=conv.id)
        assert len(msgs) == 3
        # Conversation status should still be PENDING (message doesn't auto-flip)
        fresh = await conv_repo.get_by_id(conv.id)
        assert fresh is not None
        assert fresh.status == ConversationStatus.PENDING

        # 6
        returned = await service.return_to_ai(
            tenant_id=tenant.id, conversation_id=conv.id
        )
        assert returned is not None
        assert returned.status == ConversationStatus.OPEN
        assert returned.ai_handling is True
        assert returned.assigned_agent_id is None

        # 7
        closed = await service.close(
            tenant_id=tenant.id, conversation_id=conv.id
        )
        assert closed is not None
        assert closed.status == ConversationStatus.CLOSED
        assert closed.ai_handling is False

        # 8 — find_or_create_for_inbound creates a NEW conversation
        new_conv = await service.find_or_create_for_inbound(
            tenant_id=tenant.id,
            channel_id=channel.id,
            customer_external_id="ou_lifecycle",
        )
        assert new_conv is not None
        assert new_conv.id != conv.id
        assert new_conv.status == ConversationStatus.OPEN
        assert new_conv.ai_handling is True

        # And the old closed conversation remains CLOSED
        old_again = await conv_repo.get_by_id(conv.id)
        assert old_again is not None
        assert old_again.status == ConversationStatus.CLOSED
    finally:
        await _delete_tenant(tenant.id)


# ============================================================================
# Test 5 — cross-tenant API rejection (404, not 403, not 200)
# ============================================================================


@pytest.mark.integration
async def test_cross_tenant_get_returns_404() -> None:
    """A JWT for tenant B must NOT be able to fetch a conversation owned
    by tenant A — and the response must be 404 (anti-enumeration), not
    403 and not a body that reveals existence.
    """
    tenant_a, channel_a = await _seed_tenant_and_web_channel(name="Tenant A")
    tenant_b, _channel_b_unused = await _seed_tenant_and_web_channel(name="Tenant B")
    try:
        # Seed a conversation owned by tenant A (via tenant A's WEB channel)
        service = ConversationService()
        seeded = await service.find_or_create_for_inbound(
            tenant_id=tenant_a.id,
            channel_id=channel_a.id,
            customer_external_id="ou_xtenant",
        )
        assert seeded is not None

        # Mint JWTs for both tenants
        token_a = create_access_token(
            tenant_id=tenant_a.id, user_id="u_admin_a", role="admin"
        )
        token_b = create_access_token(
            tenant_id=tenant_b.id, user_id="u_admin_b", role="admin"
        )

        # Build a FastAPI app with the conversations router
        app = FastAPI()
        app.include_router(conversations_router)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Sanity: tenant A CAN see the conversation
            resp_ok = await client.get(
                f"/api/v1/conversations/{seeded.id}",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp_ok.status_code == 200
            assert resp_ok.json()["id"] == seeded.id

            # Cross-tenant: tenant B must get 404 (not 200, not 403)
            resp_xt = await client.get(
                f"/api/v1/conversations/{seeded.id}",
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert resp_xt.status_code == 404, (
                f"cross-tenant must be 404, got {resp_xt.status_code}: {resp_xt.text}"
            )
            assert resp_xt.json()["detail"] == "conversation not found"
    finally:
        await _delete_tenant(tenant_a.id)
        await _delete_tenant(tenant_b.id)


# ============================================================================
# Test 6 — WS broadcast of AI auto-reply
# ============================================================================


@pytest.mark.asyncio
async def test_ws_broadcast_carries_ai_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """A widget WS client must receive a ``message.complete`` frame whose
    ``content`` field carries the AI auto-reply text.

    Focuses on the broadcast shape: we mock the DB and LLM layers so the
    test only asserts on the WS frame. Mirrors the structure of
    ``test_widget_e2e.test_widget_client_receives_message_complete_after_ai_response``
    but stays narrow on the broadcast assertion.
    """
    # Reset the shared WS manager singleton so the router picks up our
    # fresh instance for the broadcast path. Same pattern as the existing
    # widget e2e tests.
    import channel.inbound as inbound_module
    from widget.ws import manager as ws_manager_module
    from widget.ws import router as ws_router_module

    fresh = ConnectionManager()
    monkeypatch.setattr(ws_manager_module, "manager", fresh)
    monkeypatch.setattr(ws_router_module, "manager", fresh)
    monkeypatch.setattr(inbound_module, "_wsm", fresh)

    # A real channel so the token check passes
    ch = Channel(
        id=new_id(),
        tenant_id=new_id(),
        type=ChannelType.WEB,
        name="x",
        status=ChannelStatus.ACTIVE,
        credentials_encrypted=json.dumps({}),
        created_at=datetime.now(UTC),
    )

    async def fake_get_by_id(_self, _id):
        return ch

    monkeypatch.setattr(ChannelRepository, "get_by_id", fake_get_by_id)

    # Mock the DB: AI path must fire
    conv_id = new_id()
    ai_message_id = new_id()
    customer_msg_id = new_id()

    conv = MagicMock(
        id=conv_id,
        tenant_id=ch.tenant_id,
        ai_handling=True,
        status=ConversationStatus.OPEN,
    )
    mock_service = MagicMock()
    mock_service.find_or_create_for_inbound = AsyncMock(return_value=conv)
    mock_service.record_message = AsyncMock(
        side_effect=[
            MagicMock(id=customer_msg_id, role=MessageRole.CUSTOMER),
            MagicMock(id=ai_message_id, role=MessageRole.AI),
        ]
    )
    monkeypatch.setattr(
        "channel.inbound.ConversationService", lambda *a, **kw: mock_service
    )

    # Mock the LLM — return a fixed reply
    from agent.simple_responder import AgentResponse

    mock_responder = MagicMock()
    mock_responder.respond = AsyncMock(
        return_value=AgentResponse(content_text="AI broadcast reply", role=MessageRole.AI)
    )
    monkeypatch.setattr(
        "channel.inbound.SimpleResponder", lambda *a, **kw: mock_responder
    )

    # Mount the widget router + WS router on a fresh app
    app = FastAPI()
    app.include_router(widget_api_router)
    app.include_router(widget_ws_router)

    token, _ = create_widget_token(channel=ch, external_user_id="u_bcast")
    client = TestClient(app)
    with client.websocket_connect(f"/api/v1/widget/ws?token={token}") as ws:
        ws.send_json({"type": "message", "text": "trigger AI"})

        # Drain the next two frames. The broadcast is sent inside
        # process_inbound_envelope BEFORE the ack, but we index by frame
        # type to avoid relying on that ordering.
        frames: dict[str, dict] = {}
        for _ in range(2):
            frame = ws.receive_json()
            frames[frame["type"]] = frame

        assert "ack" in frames
        assert "message.complete" in frames

        complete = frames["message.complete"]
        # Broadcast must carry the AI reply — this is the load-bearing
        # assertion for this test.
        assert complete["content"] == "AI broadcast reply"
        assert complete["role"] == "ai"
        assert complete["conversation_id"] == conv_id
        # And the message_id is the persisted AI row's ULID
        assert complete["message_id"] == ai_message_id
        assert len(complete["message_id"]) == 26
