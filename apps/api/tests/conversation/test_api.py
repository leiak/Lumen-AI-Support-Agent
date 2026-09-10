"""Tests for the conversation REST API.

Pure-Python tests — no DB needed. We monkeypatch the auth dependency and
the `ConversationService` methods so the route handlers run against
in-memory fakes, mirroring the pattern used by
`tests/channel/test_api.py`.

Note on monkeypatching: `ConversationService.<method>` is replaced on the
class. When Python then looks up `service.method`, the descriptor protocol
binds the *instance* automatically — so our fakes must accept `self` as
their first parameter, just like a real method.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from conversation import api as api_module
from conversation import service as service_module
from conversation.api import router as conversations_router
from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation, Message
from core.id_gen import new_id

ADMIN_CLAIMS: dict[str, Any] = {
    "sub": "u_admin",
    "tenant_id": "tenant_X",
    "role": "admin",
}

AGENT_CLAIMS: dict[str, Any] = {
    "sub": "u_agent_1",
    "tenant_id": "tenant_X",
    "role": "agent",
}


async def _fake_require_admin() -> dict[str, Any]:
    """Bypass JWT verification — directly inject admin claims."""
    return ADMIN_CLAIMS


async def _fake_require_agent_or_admin() -> dict[str, Any]:
    """Bypass JWT verification — directly inject agent claims."""
    return AGENT_CLAIMS


def _conv(**kwargs: Any) -> Conversation:
    """Build a Conversation ORM instance with sensible defaults."""
    base: dict[str, Any] = dict(
        id=new_id(),
        tenant_id=ADMIN_CLAIMS["tenant_id"],
        channel_id=new_id(),
        customer_external_id="ou_customer_1",
        status=ConversationStatus.OPEN,
        assigned_agent_id=None,
        ai_handling=True,
        opened_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
        last_activity_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    )
    base.update(kwargs)
    return Conversation(**base)


def _msg(**kwargs: Any) -> Message:
    """Build a Message ORM instance with sensible defaults."""
    base: dict[str, Any] = dict(
        id=new_id(),
        conversation_id=new_id(),
        role=MessageRole.CUSTOMER,
        content_text="hello",
        content_blocks_json=None,
        sender_id=None,
        tool_calls_json=None,
        created_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    )
    base.update(kwargs)
    return Message(**base)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(conversations_router)
    return app


def _stub_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace require_admin with a bypass that returns ADMIN_CLAIMS."""
    monkeypatch.setattr(api_module, "require_admin", _fake_require_admin)


def _stub_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace require_agent_or_admin with a bypass that returns AGENT_CLAIMS."""
    monkeypatch.setattr(
        api_module, "require_agent_or_admin", _fake_require_agent_or_admin
    )


# ===========================================================================
# GET /api/v1/conversations
# ===========================================================================

@pytest.mark.asyncio
async def test_list_conversations_uses_tenant_from_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET / must pass claims['tenant_id'] to list_for_tenant, not the request."""
    _stub_admin(monkeypatch)

    captured: dict[str, Any] = {}

    async def fake_list_for_tenant(
        self: Any,
        *,
        tenant_id: str,
        status: ConversationStatus | None,
        limit: int,
        offset: int,
    ) -> list[Conversation]:
        captured["tenant_id"] = tenant_id
        captured["status"] = status
        captured["limit"] = limit
        captured["offset"] = offset
        return [_conv(), _conv(id="c2")]

    monkeypatch.setattr(
        service_module.ConversationService, "list_for_tenant", fake_list_for_tenant
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/conversations?limit=10&offset=5")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert body["limit"] == 10
    assert body["offset"] == 5
    assert len(body["items"]) == 2
    assert captured["tenant_id"] == "tenant_X"
    assert captured["status"] is None
    assert captured["limit"] == 10
    assert captured["offset"] == 5


@pytest.mark.asyncio
async def test_list_conversations_filters_by_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The optional ?status= param must reach the service."""
    _stub_admin(monkeypatch)

    captured: dict[str, Any] = {}

    async def fake_list_for_tenant(
        self: Any,
        *,
        tenant_id: str,
        status: ConversationStatus | None,
        limit: int,
        offset: int,
    ) -> list[Conversation]:
        captured["status"] = status
        return []

    monkeypatch.setattr(
        service_module.ConversationService, "list_for_tenant", fake_list_for_tenant
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/conversations?status=pending")

    assert resp.status_code == 200
    assert captured["status"] == ConversationStatus.PENDING


@pytest.mark.asyncio
async def test_list_conversations_default_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No params -> limit=50, offset=0."""
    _stub_admin(monkeypatch)

    captured: dict[str, Any] = {}

    async def fake_list_for_tenant(
        self: Any,
        *,
        tenant_id: str,
        status: ConversationStatus | None,
        limit: int,
        offset: int,
    ) -> list[Conversation]:
        captured["limit"] = limit
        captured["offset"] = offset
        return []

    monkeypatch.setattr(
        service_module.ConversationService, "list_for_tenant", fake_list_for_tenant
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/conversations")

    assert resp.status_code == 200
    assert captured["limit"] == 50
    assert captured["offset"] == 0


@pytest.mark.asyncio
async def test_list_conversations_limit_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """limit must satisfy 1<=limit<=200; out-of-range -> 422."""
    _stub_admin(monkeypatch)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/conversations?limit=999")
        assert resp.status_code == 422
        resp = await client.get("/api/v1/conversations?limit=0")
        assert resp.status_code == 422


# ===========================================================================
# GET /api/v1/conversations/inbox
# ===========================================================================

@pytest.mark.asyncio
async def test_inbox_uses_sub_claim_as_agent_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent id MUST be claims['sub'], not anything from the request."""
    _stub_agent(monkeypatch)

    captured: dict[str, Any] = {}

    async def fake_list_for_agent(
        self: Any,
        *,
        tenant_id: str,
        agent_id: str,
        status: ConversationStatus | None,
    ) -> list[Conversation]:
        captured["tenant_id"] = tenant_id
        captured["agent_id"] = agent_id
        captured["status"] = status
        return [_conv(assigned_agent_id="u_agent_1")]

    monkeypatch.setattr(
        service_module.ConversationService, "list_for_agent", fake_list_for_agent
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/conversations/inbox?status=pending")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 1
    assert captured["tenant_id"] == "tenant_X"
    assert captured["agent_id"] == "u_agent_1"
    assert captured["status"] == ConversationStatus.PENDING


@pytest.mark.asyncio
async def test_inbox_requires_agent_or_admin_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """require_agent_or_admin must reject non-agents."""
    monkeypatch.setattr(
        api_module,
        "require_agent_or_admin",
        _fake_require_admin,  # use the admin bypass so we can isolate the auth dep
    )

    # Now swap the real auth dep to one that always 403s.
    async def reject() -> dict[str, Any]:
        raise HTTPException(
            status_code=403, detail="agent or admin role required"
        )

    monkeypatch.setattr(api_module, "require_agent_or_admin", reject)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/conversations/inbox")

    assert resp.status_code == 403


# ===========================================================================
# GET /api/v1/conversations/{id}
# ===========================================================================

@pytest.mark.asyncio
async def test_get_conversation_returns_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_admin(monkeypatch)
    conv = _conv()

    async def fake_get(
        self: Any, *, tenant_id: str, conversation_id: str
    ) -> Conversation | None:
        assert tenant_id == "tenant_X"
        return conv

    monkeypatch.setattr(
        service_module.ConversationService, "get", fake_get
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/v1/conversations/{conv.id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == conv.id
    assert body["tenant_id"] == "tenant_X"
    assert body["status"] == "open"
    assert body["ai_handling"] is True


@pytest.mark.asyncio
async def test_get_conversation_404_for_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service returns None -> 404 (anti-enumeration)."""
    _stub_admin(monkeypatch)

    async def fake_get(
        self: Any, *, tenant_id: str, conversation_id: str
    ) -> Conversation | None:
        return None

    monkeypatch.setattr(
        service_module.ConversationService, "get", fake_get
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/conversations/01HX_UNKNOWN")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "conversation not found"


# ===========================================================================
# GET /api/v1/conversations/{id}/messages
# ===========================================================================

@pytest.mark.asyncio
async def test_list_messages_returns_chronological_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_admin(monkeypatch)
    conv = _conv()
    msgs = [
        _msg(conversation_id=conv.id, content_text="first"),
        _msg(conversation_id=conv.id, content_text="second"),
    ]

    captured: dict[str, Any] = {}

    async def fake_list_messages(
        self: Any,
        *,
        tenant_id: str,
        conversation_id: str,
        before: datetime | None,
        limit: int,
    ) -> list[Message] | None:
        captured["tenant_id"] = tenant_id
        captured["conversation_id"] = conversation_id
        captured["before"] = before
        captured["limit"] = limit
        return msgs

    monkeypatch.setattr(
        service_module.ConversationService, "list_messages", fake_list_messages
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/v1/conversations/{conv.id}/messages")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["items"][0]["content_text"] == "first"
    assert captured["tenant_id"] == "tenant_X"
    assert captured["conversation_id"] == conv.id
    assert captured["before"] is None
    assert captured["limit"] == 50


@pytest.mark.asyncio
async def test_list_messages_paginates_via_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_admin(monkeypatch)
    conv = _conv()
    cutoff = datetime(2026, 9, 10, 11, 0, 0, tzinfo=UTC)

    captured: dict[str, Any] = {}

    async def fake_list_messages(
        self: Any,
        *,
        tenant_id: str,
        conversation_id: str,
        before: datetime | None,
        limit: int,
    ) -> list[Message] | None:
        captured["before"] = before
        captured["limit"] = limit
        return []

    monkeypatch.setattr(
        service_module.ConversationService, "list_messages", fake_list_messages
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            f"/api/v1/conversations/{conv.id}/messages",
            params={"before": cutoff.isoformat(), "limit": 25},
        )

    assert resp.status_code == 200
    assert captured["before"] == cutoff
    assert captured["limit"] == 25


@pytest.mark.asyncio
async def test_list_messages_404_for_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service returns None (cross-tenant or not-found) -> 404."""
    _stub_admin(monkeypatch)

    async def fake_list_messages(
        self: Any,
        *,
        tenant_id: str,
        conversation_id: str,
        before: datetime | None,
        limit: int,
    ) -> list[Message] | None:
        return None

    monkeypatch.setattr(
        service_module.ConversationService, "list_messages", fake_list_messages
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/conversations/01HX_X/messages")

    assert resp.status_code == 404


# ===========================================================================
# POST /api/v1/conversations/{id}/assign
# ===========================================================================

@pytest.mark.asyncio
async def test_assign_conversation_transitions_to_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_admin(monkeypatch)
    conv = _conv()
    updated = _conv(
        id=conv.id,
        status=ConversationStatus.PENDING,
        assigned_agent_id="u_agent_1",
        ai_handling=False,
    )

    captured: dict[str, Any] = {}

    async def fake_assign(
        self: Any,
        *,
        tenant_id: str,
        conversation_id: str,
        agent_id: str,
    ) -> Conversation | None:
        captured["tenant_id"] = tenant_id
        captured["conversation_id"] = conversation_id
        captured["agent_id"] = agent_id
        return updated

    monkeypatch.setattr(
        service_module.ConversationService, "assign_to_agent", fake_assign
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/assign",
            json={"agent_id": "u_agent_1"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["assigned_agent_id"] == "u_agent_1"
    assert body["ai_handling"] is False
    assert captured["tenant_id"] == "tenant_X"
    assert captured["conversation_id"] == conv.id
    assert captured["agent_id"] == "u_agent_1"


@pytest.mark.asyncio
async def test_assign_conversation_404_for_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_admin(monkeypatch)

    async def fake_assign(
        self: Any,
        *,
        tenant_id: str,
        conversation_id: str,
        agent_id: str,
    ) -> Conversation | None:
        return None

    monkeypatch.setattr(
        service_module.ConversationService, "assign_to_agent", fake_assign
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/conversations/01HX_X/assign",
            json={"agent_id": "u_agent_1"},
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_assign_conversation_validates_agent_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty agent_id -> 422 (Pydantic validation)."""
    _stub_admin(monkeypatch)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/conversations/01HX_X/assign",
            json={"agent_id": ""},
        )

    assert resp.status_code == 422


# ===========================================================================
# POST /api/v1/conversations/{id}/return-to-ai
# ===========================================================================

@pytest.mark.asyncio
async def test_return_to_ai_resets_to_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_admin(monkeypatch)
    conv = _conv(
        status=ConversationStatus.PENDING,
        assigned_agent_id="u_agent_1",
        ai_handling=False,
    )
    updated = _conv(
        id=conv.id,
        status=ConversationStatus.OPEN,
        assigned_agent_id=None,
        ai_handling=True,
    )

    captured: dict[str, Any] = {}

    async def fake_return(
        self: Any,
        *,
        tenant_id: str,
        conversation_id: str,
    ) -> Conversation | None:
        captured["tenant_id"] = tenant_id
        captured["conversation_id"] = conversation_id
        return updated

    monkeypatch.setattr(
        service_module.ConversationService, "return_to_ai", fake_return
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/return-to-ai"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "open"
    assert body["assigned_agent_id"] is None
    assert body["ai_handling"] is True
    assert captured["tenant_id"] == "tenant_X"
    assert captured["conversation_id"] == conv.id


@pytest.mark.asyncio
async def test_return_to_ai_404_for_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_admin(monkeypatch)

    async def fake_return(
        self: Any,
        *,
        tenant_id: str,
        conversation_id: str,
    ) -> Conversation | None:
        return None

    monkeypatch.setattr(
        service_module.ConversationService, "return_to_ai", fake_return
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/conversations/01HX_X/return-to-ai")

    assert resp.status_code == 404


# ===========================================================================
# POST /api/v1/conversations/{id}/close
# ===========================================================================

@pytest.mark.asyncio
async def test_close_conversation_sets_status_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_admin(monkeypatch)
    conv = _conv()
    updated = _conv(id=conv.id, status=ConversationStatus.CLOSED, ai_handling=False)

    captured: dict[str, Any] = {}

    async def fake_close(
        self: Any,
        *,
        tenant_id: str,
        conversation_id: str,
    ) -> Conversation | None:
        captured["tenant_id"] = tenant_id
        captured["conversation_id"] = conversation_id
        return updated

    monkeypatch.setattr(
        service_module.ConversationService, "close", fake_close
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(f"/api/v1/conversations/{conv.id}/close")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "closed"
    assert body["ai_handling"] is False
    assert captured["tenant_id"] == "tenant_X"
    assert captured["conversation_id"] == conv.id


@pytest.mark.asyncio
async def test_close_conversation_404_for_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_admin(monkeypatch)

    async def fake_close(
        self: Any,
        *,
        tenant_id: str,
        conversation_id: str,
    ) -> Conversation | None:
        return None

    monkeypatch.setattr(
        service_module.ConversationService, "close", fake_close
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/conversations/01HX_X/close")

    assert resp.status_code == 404


# ===========================================================================
# Auth gates
# ===========================================================================

@pytest.mark.asyncio
async def test_admin_role_required_on_protected_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-admin role must be rejected with 403 before any service call."""

    async def reject() -> dict[str, Any]:
        raise HTTPException(status_code=403, detail="admin role required")

    monkeypatch.setattr(api_module, "require_admin", reject)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/conversations")
        assert resp.status_code == 403
        assert resp.json()["detail"] == "admin role required"


@pytest.mark.asyncio
async def test_missing_bearer_returns_401() -> None:
    """No Authorization header -> 401."""
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/conversations")
    assert resp.status_code == 401


# ===========================================================================
# End-to-end flow: get -> assign -> re-get state change
# ===========================================================================

@pytest.mark.asyncio
async def test_full_api_flow_assign_to_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET -> POST /assign -> GET again — status changes from OPEN to PENDING."""
    _stub_admin(monkeypatch)

    # State held by the fake service between calls.
    state: dict[str, Conversation] = {
        "c1": _conv(id="c1"),
    }

    async def fake_get(
        self: Any, *, tenant_id: str, conversation_id: str
    ) -> Conversation | None:
        c = state.get(conversation_id)
        if c is None or c.tenant_id != tenant_id:
            return None
        return c

    async def fake_assign(
        self: Any,
        *,
        tenant_id: str,
        conversation_id: str,
        agent_id: str,
    ) -> Conversation | None:
        c = await fake_get(self, tenant_id=tenant_id, conversation_id=conversation_id)
        if c is None:
            return None
        c.status = ConversationStatus.PENDING
        c.assigned_agent_id = agent_id
        c.ai_handling = False
        state[conversation_id] = c
        return c

    monkeypatch.setattr(service_module.ConversationService, "get", fake_get)
    monkeypatch.setattr(
        service_module.ConversationService, "assign_to_agent", fake_assign
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. Initial GET — should be OPEN
        resp = await client.get("/api/v1/conversations/c1")
        assert resp.status_code == 200
        assert resp.json()["status"] == "open"
        assert resp.json()["ai_handling"] is True

        # 2. POST /assign with body
        resp = await client.post(
            "/api/v1/conversations/c1/assign",
            json={"agent_id": "u_agent_1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "pending"
        assert body["assigned_agent_id"] == "u_agent_1"
        assert body["ai_handling"] is False

        # 3. GET again — verify state persisted
        resp = await client.get("/api/v1/conversations/c1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "pending"
        assert body["assigned_agent_id"] == "u_agent_1"
        assert body["ai_handling"] is False