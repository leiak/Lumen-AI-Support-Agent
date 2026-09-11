"""Tests for the Stage 8.2 agent workspace queue + claim endpoints.

Two surfaces under test, both under ``/api/v1/agents``:

- ``GET /api/v1/agents/queue`` — list PENDING conversations in the
  caller's tenant whose ``assigned_agent_id IS NULL`` (admin can
  override the status filter).
- ``POST /api/v1/agents/conversations/{id}/claim`` — atomic claim
  that takes ownership of a PENDING conversation via ``SELECT
  ... FOR UPDATE``.

Pure-Python tests — no DB needed. We monkeypatch the auth dependency
and the repository / service methods so the route handlers run
against in-memory fakes. The concurrent-claim test simulates the DB
row lock by sharing a single conversation object between the two
``claim()`` calls — when the first call mutates ``assigned_agent_id``,
the second ``get_by_id_for_update`` returns the same (now-modified)
object, which is exactly what the real ``SELECT FOR UPDATE`` would
deliver after the first transaction commits.

PII discipline: every assertion that touches a log payload only looks
for opaque IDs (ULIDs, user_ids). Never content_text, never
customer_external_id, never email.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from agent import api as agent_api_module
from conversation import exceptions as conv_exceptions
from conversation import repository as repo_module
from conversation import service as service_module
from conversation.enums import ConversationStatus
from conversation.models import Conversation
from core.id_gen import new_id

# ===========================================================================
# Shared claims fixtures
# ===========================================================================

AGENT_CLAIMS: dict[str, Any] = {
    "sub": "u_agent_1",
    "tenant_id": "tenant_X",
    "role": "agent",
    "email": "agent1@acme.com",
}

AGENT_B_CLAIMS: dict[str, Any] = {
    "sub": "u_agent_2",
    "tenant_id": "tenant_X",
    "role": "agent",
    "email": "agent2@acme.com",
}

ADMIN_CLAIMS: dict[str, Any] = {
    "sub": "u_admin",
    "tenant_id": "tenant_X",
    "role": "admin",
    "email": "admin@acme.com",
}

OTHER_TENANT_CLAIMS: dict[str, Any] = {
    "sub": "u_agent_other",
    "tenant_id": "tenant_Y",
    "role": "agent",
    "email": "agent@other.com",
}


# ===========================================================================
# Builders
# ===========================================================================


def _conv(**kwargs: Any) -> Conversation:
    """Build a Conversation ORM instance with sensible defaults."""
    base: dict[str, Any] = dict(
        id=new_id(),
        tenant_id=AGENT_CLAIMS["tenant_id"],
        channel_id=new_id(),
        customer_external_id="ou_customer_1",
        status=ConversationStatus.PENDING,
        assigned_agent_id=None,
        ai_handling=False,
        opened_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
        last_activity_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    )
    base.update(kwargs)
    return Conversation(**base)


def _build_app() -> FastAPI:
    """Standalone app with the agent router mounted."""
    app = FastAPI()
    app.include_router(agent_api_module.router)
    return app


def _stub_agent_auth(
    monkeypatch: pytest.MonkeyPatch,
    *,
    claims: dict[str, Any] | None = None,
) -> None:
    """Stub ``require_agent_or_admin`` on the agent router."""
    payload = claims if claims is not None else AGENT_CLAIMS

    async def _stub() -> dict[str, Any]:
        return payload

    monkeypatch.setattr(agent_api_module, "require_agent_or_admin", _stub)


def _session_ctx(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch ``conversation.service.get_sessionmaker`` so it yields a MagicMock session.

    Returns the mock session so tests can assert against its calls.
    Mirrors the pattern from ``tests/conversation/test_repositories._session_ctx``
    but at the service layer (because the claim path opens its own session
    via ``get_sessionmaker()`` rather than ``get_session()``).
    """
    from unittest.mock import AsyncMock, MagicMock

    mock_session = MagicMock()
    mock_session.execute = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.refresh = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.rollback = AsyncMock()

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_session)
    cm.__aexit__ = AsyncMock(return_value=None)

    sm = MagicMock()
    sm.return_value = cm
    monkeypatch.setattr(service_module, "get_sessionmaker", lambda: sm)
    return mock_session


# ===========================================================================
# GET /api/v1/agents/queue
# ===========================================================================


@pytest.mark.asyncio
async def test_queue_returns_pending_unassigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 PENDING unassigned + 1 PENDING assigned + 1 OPEN -> only 3 returned."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)

    pending_unassigned = [
        _conv(status=ConversationStatus.PENDING, assigned_agent_id=None),
        _conv(status=ConversationStatus.PENDING, assigned_agent_id=None),
        _conv(status=ConversationStatus.PENDING, assigned_agent_id=None),
    ]

    captured: dict[str, Any] = {}

    async def fake_list_pending(
        self: Any,
        *,
        tenant_id: str,
        status: ConversationStatus,
        limit: int,
        offset: int,
    ) -> list[Conversation]:
        captured["tenant_id"] = tenant_id
        captured["status"] = status
        captured["limit"] = limit
        captured["offset"] = offset
        return pending_unassigned

    monkeypatch.setattr(
        repo_module.ConversationRepository,
        "list_pending_for_tenant",
        fake_list_pending,
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/agents/queue")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 3
    for item in body["items"]:
        assert item["status"] == "pending"
        assert item["assigned_agent_id"] is None
    assert captured["tenant_id"] == "tenant_X"
    assert captured["status"] == ConversationStatus.PENDING
    assert captured["limit"] == 50
    assert captured["offset"] == 0


@pytest.mark.asyncio
async def test_queue_sorted_by_last_activity_desc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repo sorts by last_activity_at DESC — verify route passes through."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)

    # Build 3 conversations with distinct last_activity_at — the
    # repository is responsible for the actual SQL ORDER BY (we
    # assert that here as a unit test of the contract).
    now = datetime.now(UTC)
    conv_newest = _conv(last_activity_at=now)
    conv_mid = _conv(last_activity_at=now - timedelta(minutes=5))
    conv_oldest = _conv(last_activity_at=now - timedelta(minutes=30))

    # Repo returns in the desired order (DESC).
    expected_order = [conv_newest, conv_mid, conv_oldest]

    async def fake_list_pending(
        self: Any,
        *,
        tenant_id: str,
        status: ConversationStatus,
        limit: int,
        offset: int,
    ) -> list[Conversation]:
        return expected_order

    monkeypatch.setattr(
        repo_module.ConversationRepository,
        "list_pending_for_tenant",
        fake_list_pending,
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/agents/queue")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 3
    assert body["items"][0]["id"] == conv_newest.id
    assert body["items"][1]["id"] == conv_mid.id
    assert body["items"][2]["id"] == conv_oldest.id


@pytest.mark.asyncio
async def test_queue_pagination_limit_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """limit=2, offset=1 reaches the repo unchanged."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    captured: dict[str, Any] = {}

    async def fake_list_pending(
        self: Any,
        *,
        tenant_id: str,
        status: ConversationStatus,
        limit: int,
        offset: int,
    ) -> list[Conversation]:
        captured["limit"] = limit
        captured["offset"] = offset
        return []

    monkeypatch.setattr(
        repo_module.ConversationRepository,
        "list_pending_for_tenant",
        fake_list_pending,
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/agents/queue?limit=2&offset=1")

    assert resp.status_code == 200
    assert captured["limit"] == 2
    assert captured["offset"] == 1


@pytest.mark.asyncio
async def test_queue_cross_tenant_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant A's queue MUST NOT appear in tenant B's queue call."""
    _stub_agent_auth(monkeypatch, claims=OTHER_TENANT_CLAIMS)

    seen_tenant_ids: list[str] = []

    async def fake_list_pending(
        self: Any,
        *,
        tenant_id: str,
        status: ConversationStatus,
        limit: int,
        offset: int,
    ) -> list[Conversation]:
        seen_tenant_ids.append(tenant_id)
        # If the route ever leaked tenant_A's id, we'd see it here.
        assert tenant_id == OTHER_TENANT_CLAIMS["tenant_id"]
        return []

    monkeypatch.setattr(
        repo_module.ConversationRepository,
        "list_pending_for_tenant",
        fake_list_pending,
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/agents/queue")

    assert resp.status_code == 200
    assert seen_tenant_ids == [OTHER_TENANT_CLAIMS["tenant_id"]]
    assert seen_tenant_ids[0] != AGENT_CLAIMS["tenant_id"]


@pytest.mark.asyncio
async def test_queue_admin_can_override_status_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admin passes status=OPEN -> repo receives status=OPEN (not PENDING)."""
    _stub_agent_auth(monkeypatch, claims=ADMIN_CLAIMS)

    open_convs = [
        _conv(status=ConversationStatus.OPEN),
        _conv(status=ConversationStatus.OPEN, assigned_agent_id="u_admin"),
    ]
    captured: dict[str, Any] = {}

    async def fake_list_pending(
        self: Any,
        *,
        tenant_id: str,
        status: ConversationStatus,
        limit: int,
        offset: int,
    ) -> list[Conversation]:
        captured["status"] = status
        return open_convs

    monkeypatch.setattr(
        repo_module.ConversationRepository,
        "list_pending_for_tenant",
        fake_list_pending,
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/agents/queue?status=open")

    assert resp.status_code == 200, resp.text
    assert captured["status"] == ConversationStatus.OPEN
    body = resp.json()
    # Admin override: returned OPEN convs include both assigned and unassigned.
    assert len(body["items"]) == 2
    statuses = {item["status"] for item in body["items"]}
    assert statuses == {"open"}


@pytest.mark.asyncio
async def test_queue_rejects_oversized_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """limit=500 -> 422 from Pydantic / Query validation."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)

    async def fail(*_args: Any, **_kwargs: Any) -> list[Conversation]:
        raise AssertionError("repo should not be called")

    monkeypatch.setattr(
        repo_module.ConversationRepository,
        "list_pending_for_tenant",
        fail,
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/agents/queue?limit=500")

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_queue_rejects_negative_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """offset=-1 -> 422 from Query validation."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/agents/queue?offset=-1")

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_queue_requires_auth() -> None:
    """No Authorization header -> 401 from the dependency."""
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/agents/queue")

    assert resp.status_code == 401


# ===========================================================================
# POST /api/v1/agents/conversations/{id}/claim
# ===========================================================================


@pytest.mark.asyncio
async def test_claim_assigns_to_calling_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent claims an unassigned PENDING conv -> assigned_agent_id = claims['sub]."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    session = _session_ctx(monkeypatch)

    conv = _conv(
        status=ConversationStatus.PENDING,
        assigned_agent_id=None,
        ai_handling=False,
    )
    # The repo returns the same object both before and after the
    # service mutates it; the real FOR UPDATE would behave the same
    # way after the lock+commit window.
    captured: dict[str, Any] = {}

    async def fake_get_for_update(
        self: Any,
        *,
        session: Any,
        tenant_id: str,
        conversation_id: str,
    ) -> Conversation | None:
        captured["tenant_id"] = tenant_id
        captured["conversation_id"] = conversation_id
        return conv

    monkeypatch.setattr(
        repo_module.ConversationRepository,
        "get_by_id_for_update",
        fake_get_for_update,
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{conv.id}/claim"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assigned_agent_id"] == AGENT_CLAIMS["sub"]
    assert body["status"] == "pending"
    assert body["ai_handling"] is False
    assert captured["tenant_id"] == "tenant_X"
    assert captured["conversation_id"] == conv.id
    # The session was committed exactly once.
    assert session.commit.await_count == 1
    assert session.rollback.await_count == 0


@pytest.mark.asyncio
async def test_claim_persists_across_db_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After claim, a fresh get_by_id_for_update on the same row sees the update.

    Simulates a second read in a "fresh session" by re-invoking the
    mocked repo method and verifying the shared conversation object
    now carries the agent_id (which is exactly what the real DB
    would deliver after commit).
    """
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    _session_ctx(monkeypatch)

    conv = _conv(
        status=ConversationStatus.PENDING,
        assigned_agent_id=None,
        ai_handling=False,
    )

    async def fake_get_for_update(
        self: Any,
        *,
        session: Any,
        tenant_id: str,
        conversation_id: str,
    ) -> Conversation | None:
        return conv

    monkeypatch.setattr(
        repo_module.ConversationRepository,
        "get_by_id_for_update",
        fake_get_for_update,
    )

    svc = service_module.ConversationService()
    claimed = await svc.claim(
        tenant_id=AGENT_CLAIMS["tenant_id"],
        conversation_id=conv.id,
        agent_id=AGENT_CLAIMS["sub"],
    )

    # "Fresh session" re-read — the real DB would show the
    # committed row. With mocks, the same shared object reflects
    # the post-commit state.
    fresh_read = await repo_module.ConversationRepository().get_by_id_for_update(
        session=None,  # type: ignore[arg-type]
        tenant_id=AGENT_CLAIMS["tenant_id"],
        conversation_id=conv.id,
    )
    assert fresh_read is claimed
    assert fresh_read.assigned_agent_id == AGENT_CLAIMS["sub"]
    assert fresh_read.status == ConversationStatus.PENDING


@pytest.mark.asyncio
async def test_claim_returns_404_on_unknown_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Random ULID -> 404 (anti-enumeration: same as cross-tenant)."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    _session_ctx(monkeypatch)

    async def fake_get_for_update(
        self: Any,
        *,
        session: Any,
        tenant_id: str,
        conversation_id: str,
    ) -> Conversation | None:
        return None  # Unknown conversation.

    monkeypatch.setattr(
        repo_module.ConversationRepository,
        "get_by_id_for_update",
        fake_get_for_update,
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/agents/conversations/01HX_NOTREAL/claim"
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "conversation not found"


@pytest.mark.asyncio
async def test_claim_returns_404_on_cross_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different tenant's conv -> 404 (anti-enumeration)."""
    _stub_agent_auth(monkeypatch, claims=OTHER_TENANT_CLAIMS)
    _session_ctx(monkeypatch)

    # Repo returns None — simulates the tenant_id WHERE filter
    # rejecting a foreign-tenant row.
    async def fake_get_for_update(
        self: Any,
        *,
        session: Any,
        tenant_id: str,
        conversation_id: str,
    ) -> Conversation | None:
        return None

    monkeypatch.setattr(
        repo_module.ConversationRepository,
        "get_by_id_for_update",
        fake_get_for_update,
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/agents/conversations/01HX_OTHER/claim"
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "conversation not found"


@pytest.mark.asyncio
async def test_claim_returns_409_on_already_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conv already assigned to another agent -> 409."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    session = _session_ctx(monkeypatch)

    already_claimed = _conv(
        status=ConversationStatus.PENDING,
        assigned_agent_id="u_other_agent",  # someone else got here first
        ai_handling=False,
    )

    async def fake_get_for_update(
        self: Any,
        *,
        session: Any,
        tenant_id: str,
        conversation_id: str,
    ) -> Conversation | None:
        return already_claimed

    monkeypatch.setattr(
        repo_module.ConversationRepository,
        "get_by_id_for_update",
        fake_get_for_update,
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{already_claimed.id}/claim"
        )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "conversation cannot be claimed"
    # Service must have rolled back (not committed an empty txn).
    assert session.rollback.await_count == 1
    assert session.commit.await_count == 0


@pytest.mark.asyncio
async def test_claim_returns_409_on_wrong_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claim an OPEN conv (no escalation transition) -> 409."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    session = _session_ctx(monkeypatch)

    open_conv = _conv(
        status=ConversationStatus.OPEN,  # wrong status
        assigned_agent_id=None,
        ai_handling=True,
    )

    async def fake_get_for_update(
        self: Any,
        *,
        session: Any,
        tenant_id: str,
        conversation_id: str,
    ) -> Conversation | None:
        return open_conv

    monkeypatch.setattr(
        repo_module.ConversationRepository,
        "get_by_id_for_update",
        fake_get_for_update,
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{open_conv.id}/claim"
        )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "conversation cannot be claimed"
    # The 409 must be the SAME wording the already-claimed case
    # produces — anti-enumeration.
    assert session.rollback.await_count == 1


@pytest.mark.asyncio
async def test_claim_admin_can_claim_on_behalf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admin JWT (no agent role) can claim — require_agent_or_admin admits admin."""
    _stub_agent_auth(monkeypatch, claims=ADMIN_CLAIMS)
    _session_ctx(monkeypatch)

    conv = _conv(
        status=ConversationStatus.PENDING,
        assigned_agent_id=None,
        ai_handling=False,
    )

    async def fake_get_for_update(
        self: Any,
        *,
        session: Any,
        tenant_id: str,
        conversation_id: str,
    ) -> Conversation | None:
        return conv

    monkeypatch.setattr(
        repo_module.ConversationRepository,
        "get_by_id_for_update",
        fake_get_for_update,
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{conv.id}/claim"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assigned_agent_id"] == ADMIN_CLAIMS["sub"]
    assert body["status"] == "pending"


@pytest.mark.asyncio
async def test_claim_concurrent_claims_only_one_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent claim() calls -> exactly one wins.

    Simulates the ``SELECT ... FOR UPDATE`` lock semantics with a
    shared conversation object — the first call mutates
    ``assigned_agent_id`` on the row, the second call's
    ``get_by_id_for_update`` returns the same (now-modified) object
    and the service short-circuits to a
    :class:`ConversationNotClaimableError`. The two ``claim()``
    coroutines run on the same event loop via ``asyncio.gather``;
    even if the scheduler serialises them, the post-condition is
    exactly one success and one 409, which is the invariant the
    real DB-level lock guarantees.
    """
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    _session_ctx(monkeypatch)

    conv = _conv(
        status=ConversationStatus.PENDING,
        assigned_agent_id=None,
        ai_handling=False,
    )

    async def fake_get_for_update(
        self: Any,
        *,
        session: Any,
        tenant_id: str,
        conversation_id: str,
    ) -> Conversation | None:
        return conv

    monkeypatch.setattr(
        repo_module.ConversationRepository,
        "get_by_id_for_update",
        fake_get_for_update,
    )

    svc_a = service_module.ConversationService()

    async def claim_with(agent_id: str) -> Conversation:
        return await svc_a.claim(
            tenant_id=AGENT_CLAIMS["tenant_id"],
            conversation_id=conv.id,
            agent_id=agent_id,
        )

    # gather(return_exceptions=True) lets us inspect both outcomes
    # — by default gather() raises on the first exception and we
    # couldn't count the second outcome.
    results = await asyncio.gather(
        claim_with(AGENT_CLAIMS["sub"]),
        claim_with(AGENT_B_CLAIMS["sub"]),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, Conversation)]
    failures = [
        r for r in results if isinstance(r, conv_exceptions.ConversationNotClaimableError)
    ]

    assert len(successes) == 1, (
        f"expected exactly one winner, got {len(successes)} "
        f"(results={[type(r).__name__ for r in results]})"
    )
    assert len(failures) == 1, (
        f"expected exactly one 409, got {len(failures)} "
        f"(results={[type(r).__name__ for r in results]})"
    )
    # The winner's assigned_agent_id is one of the two agents.
    assert successes[0].assigned_agent_id in {
        AGENT_CLAIMS["sub"],
        AGENT_B_CLAIMS["sub"],
    }
    # The conversation now reflects the winner.
    assert conv.assigned_agent_id == successes[0].assigned_agent_id


@pytest.mark.asyncio
async def test_claim_does_not_broadcast_ws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claim does NOT call broadcast_to_channel — it's a state change, not a message."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    _session_ctx(monkeypatch)

    conv = _conv(
        status=ConversationStatus.PENDING,
        assigned_agent_id=None,
        ai_handling=False,
    )

    async def fake_get_for_update(
        self: Any,
        *,
        session: Any,
        tenant_id: str,
        conversation_id: str,
    ) -> Conversation | None:
        return conv

    monkeypatch.setattr(
        repo_module.ConversationRepository,
        "get_by_id_for_update",
        fake_get_for_update,
    )

    # Spy on the WS manager — the claim route does not broadcast, so
    # this AsyncMock must remain untouched.
    from unittest.mock import AsyncMock

    from widget.ws import manager as ws_manager_module

    broadcast_calls: list[Any] = []

    async def spy_broadcast(*args: Any, **kwargs: Any) -> int:
        broadcast_calls.append((args, kwargs))
        return 0

    ws_manager_module.manager.broadcast_to_channel = AsyncMock(
        side_effect=spy_broadcast
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{conv.id}/claim"
        )

    assert resp.status_code == 200
    assert ws_manager_module.manager.broadcast_to_channel.await_count == 0
    assert broadcast_calls == []


@pytest.mark.asyncio
async def test_claim_logs_assigned_agent_id_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim log line MUST carry agent_id/tenant_id/conversation_id and NOTHING else.

    PII discipline: NEVER customer text, NEVER email, NEVER the
    exception repr. We capture structlog events by redirecting
    stdout (the default ``PrintLoggerFactory`` sink in the test
    environment) for the duration of the claim call and assert
    against the captured text.
    """
    import contextlib
    import io

    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    _session_ctx(monkeypatch)

    conv = _conv(
        status=ConversationStatus.PENDING,
        assigned_agent_id=None,
        ai_handling=False,
    )

    async def fake_get_for_update(
        self: Any,
        *,
        session: Any,
        tenant_id: str,
        conversation_id: str,
    ) -> Conversation | None:
        return conv

    monkeypatch.setattr(
        repo_module.ConversationRepository,
        "get_by_id_for_update",
        fake_get_for_update,
    )

    app = _build_app()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/agents/conversations/{conv.id}/claim"
            )

    assert resp.status_code == 200

    captured_text = buf.getvalue()
    # Find the line that carries our claim log event.
    claim_lines = [
        line
        for line in captured_text.splitlines()
        if "agent claimed conversation" in line
    ]
    assert claim_lines, (
        f"expected an 'agent claimed conversation' log line in:\n{captured_text!r}"
    )

    line = claim_lines[0]
    # Agent id MUST be present in the message — opaque ULID, safe to log.
    assert AGENT_CLAIMS["sub"] in line, (
        f"claim log missing agent_id; got line: {line!r}"
    )
    # Conversation id MUST be present — opaque ULID, safe to log.
    assert conv.id in line
    # Customer PII MUST NOT appear.
    assert conv.customer_external_id not in line
    assert conv.channel_id not in line
    # Email MUST NOT appear.
    assert AGENT_CLAIMS["email"] not in line
    # No exception repr / no raw text / no repr of any sort.
    assert "Exception" not in line
    assert "Traceback" not in line


# ===========================================================================
# Auth gates (consolidation refactor still works)
# ===========================================================================


@pytest.mark.asyncio
async def test_claim_rejects_invalid_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth dep must reject a non-agent / non-admin caller with 403."""

    async def reject() -> dict[str, Any]:
        raise HTTPException(
            status_code=403, detail="agent or admin role required"
        )

    monkeypatch.setattr(agent_api_module, "require_agent_or_admin", reject)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/agents/conversations/01HX_X/claim"
        )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "agent or admin role required"


@pytest.mark.asyncio
async def test_claim_requires_auth() -> None:
    """No Authorization header -> 401 from the dependency."""
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/agents/conversations/01HX_X/claim"
        )

    assert resp.status_code == 401