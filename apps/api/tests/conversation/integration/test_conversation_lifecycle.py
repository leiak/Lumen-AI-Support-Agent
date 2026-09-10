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

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from agent import simple_responder
from auth.jwt import create_access_token
from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from channel.repository import ChannelRepository
from conversation.api import router as conversations_router
from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation, Message
from conversation.repository import ConversationRepository, MessageRepository
from conversation.service import ConversationService
from core.database import get_session
from core.id_gen import new_id
from llm_client.types import ChatResponse
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository
from widget.adapter import WebWidgetAdapter
from widget.ws.manager import ConnectionManager

# ============================================================================
# Fixtures / helpers
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_db_singletons(monkeypatch: pytest.MonkeyPatch):
    """Reset async engine/sessionmaker between tests to avoid cross-loop
    contamination, AND rebind the shared ``ConnectionManager`` singleton to a
    fresh instance.

    The WS manager is shared by ``widget.ws.router`` (which owns the
    connection lifecycle) and ``channel.inbound`` (which broadcasts
    server-initiated events). Every module-level alias must be rebound to
    the SAME fresh instance — otherwise a broadcast targets a different
    connection table than the one the socket registered itself in, and
    silently delivers to nobody. Same pattern as
    ``tests/widget/integration/test_widget_e2e._reset_singletons``.
    """
    import channel.inbound as inbound_module
    from core.database import reset_engine, reset_sessionmaker
    from widget.ws import manager as ws_manager_module
    from widget.ws import router as ws_router_module

    fresh = ConnectionManager()
    monkeypatch.setattr(ws_manager_module, "manager", fresh)
    monkeypatch.setattr(ws_router_module, "manager", fresh)
    monkeypatch.setattr(inbound_module, "_wsm", fresh)

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
        # NOTE — best-effort race coverage, not a deterministic race test.
        # ``asyncio.Barrier(2)`` + ``asyncio.gather`` only synchronises the
        # *starts* of the two coroutines on the same event loop; in practice
        # one of them almost always finishes its find-then-create window
        # before the other even reaches the SELECT. The post-condition
        # (exactly one OPEN row, both callers see the same id) is what we
        # are actually exercising — both paths (winner-and-skip and
        # IntegrityError-and-recover) yield the same observable state, so a
        # green run here confirms only that ``find_or_create_for_inbound``
        # is internally consistent under concurrent invocation.
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
        # last_activity_at must be the same wall-clock instant. Use a
        # sub-millisecond tolerance for forward-compat with future column-level
        # ``func.now()`` defaults that may introduce a tiny gap between the
        # two writes.
        assert (
            abs((recorded.created_at - conv.last_activity_at).total_seconds()) < 0.001
        ), (
            f"created_at={recorded.created_at} last_activity_at={conv.last_activity_at}"
        )
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
        # Clean up both tenants in parallel for a small speedup.
        await asyncio.gather(
            _delete_tenant(tenant_a.id),
            _delete_tenant(tenant_b.id),
        )


# ============================================================================
# Test 6 — WS broadcast of AI auto-reply (DB-backed)
# ============================================================================


@pytest.mark.integration
async def test_ws_broadcast_carries_ai_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """A widget ``message.complete`` broadcast must carry a REAL ULID that
    exists in the ``messages`` table.

    Unlike the mocked coverage in
    ``tests/widget/integration/test_widget_e2e.py`` — which proves the
    broadcast *shape* with a stubbed ``ConversationService`` — this test
    proves the conversation lifecycle actually produces a durable row
    before the broadcast is emitted.

    Implementation note — we drive ``process_inbound_envelope`` directly
    from the test's main loop rather than round-tripping through the WS
    endpoint. The WS endpoint runs inside ``TestClient``'s anyio portal,
    which lives on a separate event loop from the test; the test's
    ``asyncpg`` engine is bound to the test's loop, so any DB call from
    inside the portal raises ``RuntimeError: got Future ... attached to a
    different loop``. Driving the pipeline from the test's loop avoids
    that while still exercising the real ``ConversationService``,
    repositories, and ``SimpleResponder.respond`` path. We capture the
    broadcast by monkey-patching ``ConnectionManager.broadcast_to_channel``
    on the fresh singleton (rebound by the autouse fixture), so we
    observe exactly the payload that ``_broadcast_ai_complete`` would have
    delivered to a live socket.

    The only mock is the LLMClient factory (swapped via
    ``agent.simple_responder._default_llm_client_factory``) — we never
    want a real Anthropic call in tests. ``ConversationService`` and the
    repositories run unchanged against the live DB.
    """
    tenant, channel = await _seed_tenant_and_web_channel(name="DB-backed WS Tenant")
    try:
        # Swap the LLMClient factory so SimpleResponder returns a canned
        # response. We do NOT mock SimpleResponder itself — the call to
        # ``responder.respond()`` runs for real and exercises the
        # history-build + agent_response shape.
        fake_reply_text = "AI broadcast reply from real DB lifecycle"

        class _FakeLLMClient:
            async def chat(self, _request: object, *, max_retries: int = 3) -> ChatResponse:
                return ChatResponse(
                    content=fake_reply_text,
                    model="fake-model",
                    prompt_tokens=10,
                    completion_tokens=5,
                    finish_reason="stop",
                )

        monkeypatch.setattr(
            simple_responder,
            "_default_llm_client_factory",
            lambda _tenant_id: _FakeLLMClient(),
        )

        # Capture broadcasts via the manager singleton that the autouse
        # fixture just rebound. We still call through to the real
        # ``broadcast_to_channel`` so any actual delivery (to zero
        # connections, since this test never opens a WS) behaves
        # identically to production.
        from widget.ws import manager as ws_manager_module

        captured_broadcasts: list[dict[str, object]] = []
        real_broadcast = ws_manager_module.manager.broadcast_to_channel

        async def capture_broadcast(
            channel_id: str, payload: dict[str, object], *, exclude: str | None = None
        ) -> int:
            captured_broadcasts.append(
                {"channel_id": channel_id, "payload": dict(payload)}
            )
            return await real_broadcast(channel_id, payload, exclude=exclude)

        monkeypatch.setattr(
            ws_manager_module.manager,
            "broadcast_to_channel",
            capture_broadcast,
        )

        # Parse an inbound frame via the widget adapter — the same code
        # path the WS endpoint uses to turn a raw ``{"type": "message",
        # ...}`` frame into a canonical MessageEnvelope.
        adapter = WebWidgetAdapter()
        frame = {
            "type": "message",
            "text": "trigger AI",
            "external_user_id": "u_db_bcast",
            "client_message_id": "cm_db_bcast_1",
        }
        envelope = await adapter.parse_inbound(raw=frame, channel=channel)

        # Drive the full inbound pipeline from the test's main loop.
        # ``process_inbound_envelope`` does:
        #   find_or_create_for_inbound -> record_message(CUSTOMER)
        #   -> SimpleResponder.respond -> record_message(AI)
        #   -> _broadcast_ai_complete -> _wsm.broadcast_to_channel.
        from channel.inbound import process_inbound_envelope

        await process_inbound_envelope(envelope)

        # The broadcast was captured exactly once.
        assert len(captured_broadcasts) == 1, (
            f"expected exactly 1 broadcast, got {len(captured_broadcasts)}"
        )
        broadcast = captured_broadcasts[0]
        assert broadcast["channel_id"] == channel.id

        payload = broadcast["payload"]
        # WS frame shape — the same dict that
        # ``ConnectionManager.send_to_connection`` would forward to a
        # connected socket.
        assert payload["type"] == "message.complete"
        assert payload["content"] == fake_reply_text
        assert payload["role"] == "ai"
        assert len(str(payload["conversation_id"])) == 26

        # Load-bearing assertion: the broadcast message_id is a REAL
        # ULID that exists in the messages table. This is what makes
        # this test additive over the widget e2e mocks — we prove the
        # lifecycle produced the row before broadcasting.
        ai_msg_id = str(payload["message_id"])
        assert len(ai_msg_id) == 26

        async with get_session() as session:
            msg = await session.get(Message, ai_msg_id)
            assert msg is not None, (
                f"broadcast message_id {ai_msg_id} not present in messages table"
            )
            assert msg.role == MessageRole.AI
            assert msg.content_text == fake_reply_text
            # And the customer message was persisted too — proves the
            # full customer-message + AI-reply + broadcast chain ran.
            stmt = select(Message).where(
                Message.conversation_id == msg.conversation_id
            )
            result = await session.execute(stmt)
            all_msgs = list(result.scalars().all())
            roles = sorted(m.role for m in all_msgs)
            assert roles == sorted([MessageRole.CUSTOMER, MessageRole.AI]), roles
            assert len(all_msgs) == 2
    finally:
        await _delete_tenant(tenant.id)
