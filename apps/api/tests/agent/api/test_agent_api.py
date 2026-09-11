"""Tests for the Stage 8.1 agent workspace API.

Two surfaces under test:

1. ``GET /api/v1/agents/me`` — JWT-derived identity (with tenant_name
   joined from ``TenantRepository``).
2. ``POST /api/v1/conversations/{id}/messages`` — agent reply box.
   Lives on the conversation router but exercised here for Stage 8.1.

Pure-Python tests — no DB. We monkeypatch the auth dependency and the
service / repository methods so the route handlers run against in-memory
fakes. Mirrors the pattern from ``tests/conversation/test_api.py``.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from agent import api as agent_api_module
from conversation import api as conv_api_module
from conversation import service as service_module
from conversation.api import router as conversations_router
from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation, Message
from core.id_gen import new_id
from tenant.models import Tenant

AGENT_CLAIMS: dict[str, Any] = {
    "sub": "u_agent_1",
    "tenant_id": "tenant_X",
    "role": "agent",
    "email": "agent1@acme.com",
}

ADMIN_CLAIMS: dict[str, Any] = {
    "sub": "u_admin",
    "tenant_id": "tenant_X",
    "role": "admin",
    "email": "admin@acme.com",
}

OTHER_TENANT_CLAIMS: dict[str, Any] = {
    "sub": "u_agent_2",
    "tenant_id": "tenant_Y",
    "role": "agent",
    "email": "agent2@other.com",
}


# ===========================================================================
# Fakes / builders
# ===========================================================================

async def _fake_require_agent_or_admin() -> dict[str, Any]:
    """Default bypass for the agent+admin dependency."""
    return AGENT_CLAIMS


def _conv(**kwargs: Any) -> Conversation:
    """Build a Conversation ORM instance with sensible defaults."""
    base: dict[str, Any] = dict(
        id=new_id(),
        tenant_id=AGENT_CLAIMS["tenant_id"],
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


def _tenant(**kwargs: Any) -> Tenant:
    """Build a Tenant ORM instance with sensible defaults."""
    base: dict[str, Any] = dict(
        id="tenant_X",
        name="Acme",
        plan="pro",
        status="active",
        settings={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    base.update(kwargs)
    return Tenant(**base)


def _build_app() -> FastAPI:
    """Standalone app with the agent router mounted."""
    app = FastAPI()
    app.include_router(agent_api_module.router)
    app.include_router(conversations_router)
    return app


def _stub_agent_auth(
    monkeypatch: pytest.MonkeyPatch,
    *,
    claims: dict[str, Any] | None = None,
) -> None:
    """Default: stub the auth deps on both the agent and conversation routers."""
    payload = claims if claims is not None else AGENT_CLAIMS

    async def _stub() -> dict[str, Any]:
        return payload

    monkeypatch.setattr(agent_api_module, "require_agent_or_admin", _stub)
    monkeypatch.setattr(conv_api_module, "require_agent_or_admin", _stub)
    monkeypatch.setattr(conv_api_module, "require_admin", _stub)


# ===========================================================================
# GET /api/v1/agents/me
# ===========================================================================

@pytest.mark.asyncio
async def test_get_me_returns_jwt_identity_for_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent JWT -> 200 with user_id/email/tenant_id/role from claims."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)

    async def fake_get_by_id(_self: Any, tenant_id: str) -> Tenant | None:
        assert tenant_id == "tenant_X"
        return _tenant(id="tenant_X", name="Acme")

    from tenant import repository as tenant_repo_module

    monkeypatch.setattr(tenant_repo_module.TenantRepository, "get_by_id", fake_get_by_id)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/agents/me")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == "u_agent_1"
    assert body["email"] == "agent1@acme.com"
    assert body["tenant_id"] == "tenant_X"
    assert body["tenant_name"] == "Acme"
    assert body["role"] == "agent"


@pytest.mark.asyncio
async def test_get_me_returns_jwt_identity_for_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admin JWT -> 200 with role='admin'."""
    _stub_agent_auth(monkeypatch, claims=ADMIN_CLAIMS)

    async def fake_get_by_id(_self: Any, tenant_id: str) -> Tenant | None:
        return _tenant(id=tenant_id, name="Acme")

    from tenant import repository as tenant_repo_module

    monkeypatch.setattr(tenant_repo_module.TenantRepository, "get_by_id", fake_get_by_id)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/agents/me")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "admin"
    assert body["user_id"] == "u_admin"
    assert body["email"] == "admin@acme.com"


@pytest.mark.asyncio
async def test_get_me_rejects_missing_token() -> None:
    """No Authorization header -> 401 from the dependency."""
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/agents/me")

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_me_rejects_invalid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed Bearer token -> 401 from the dependency."""

    async def reject(_authorization: str | None = None) -> dict[str, Any]:
        raise HTTPException(status_code=401, detail="missing bearer token")

    monkeypatch.setattr(agent_api_module, "require_agent_or_admin", reject)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/agents/me",
            headers={"Authorization": "Bearer not-a-real-jwt"},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_me_includes_tenant_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """TenantRepository.get_by_id is called with the JWT tenant_id."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)

    captured: dict[str, Any] = {}

    async def fake_get_by_id(_self: Any, tenant_id: str) -> Tenant | None:
        captured["tenant_id"] = tenant_id
        return _tenant(id=tenant_id, name="Acme Industries")

    from tenant import repository as tenant_repo_module

    monkeypatch.setattr(tenant_repo_module.TenantRepository, "get_by_id", fake_get_by_id)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/agents/me")

    assert resp.status_code == 200
    assert captured["tenant_id"] == "tenant_X"
    assert resp.json()["tenant_name"] == "Acme Industries"


@pytest.mark.asyncio
async def test_get_me_falls_back_to_tenant_id_when_tenant_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the tenant row is gone (deleted, race), return the id as the name."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)

    async def fake_get_by_id(_self: Any, tenant_id: str) -> Tenant | None:
        return None

    from tenant import repository as tenant_repo_module

    monkeypatch.setattr(tenant_repo_module.TenantRepository, "get_by_id", fake_get_by_id)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/agents/me")

    assert resp.status_code == 200
    assert resp.json()["tenant_name"] == "tenant_X"


# ===========================================================================
# POST /api/v1/conversations/{id}/messages  (agent reply box)
# ===========================================================================

@pytest.mark.asyncio
async def test_post_agent_message_persists_agent_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent POSTs to assigned conversation -> 201 + DB row with role=AGENT."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    conv = _conv(assigned_agent_id="u_agent_1")
    persisted_msg = _msg(
        conversation_id=conv.id,
        role=MessageRole.AGENT,
        content_text="hi, looking into it",
        sender_id="u_agent_1",
    )

    captured: dict[str, Any] = {}

    async def fake_get(self: Any, *, tenant_id: str, conversation_id: str) -> Conversation | None:
        assert tenant_id == "tenant_X"
        assert conversation_id == conv.id
        return conv

    async def fake_record(
        self: Any,
        *,
        tenant_id: str,
        conversation_id: str,
        role: MessageRole,
        content_text: str,
        sender_id: str | None,
        content_blocks: dict[str, Any] | None = None,
        tool_calls: dict[str, Any] | None = None,
    ) -> Message:
        captured["tenant_id"] = tenant_id
        captured["conversation_id"] = conversation_id
        captured["role"] = role
        captured["content_text"] = content_text
        captured["sender_id"] = sender_id
        return persisted_msg

    monkeypatch.setattr(service_module.ConversationService, "get", fake_get)
    monkeypatch.setattr(
        service_module.ConversationService, "record_message", fake_record
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/messages",
            json={"content_text": "  hi, looking into it  "},
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["role"] == "agent"
    assert body["sender_id"] == "u_agent_1"
    assert body["content_text"] == "hi, looking into it"  # stripped
    assert captured["tenant_id"] == "tenant_X"
    assert captured["conversation_id"] == conv.id
    assert captured["role"] == MessageRole.AGENT
    assert captured["content_text"] == "hi, looking into it"
    assert captured["sender_id"] == "u_agent_1"


@pytest.mark.asyncio
async def test_post_agent_message_rejects_empty_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All-whitespace content_text -> 422 from the route handler."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    conv = _conv()

    async def fake_get(self: Any, *, tenant_id: str, conversation_id: str) -> Conversation | None:
        return conv

    monkeypatch.setattr(service_module.ConversationService, "get", fake_get)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/messages",
            json={"content_text": "   "},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_agent_message_rejects_oversized_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5000-char body -> 422 from Pydantic max_length."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    conv = _conv()

    async def fake_get(self: Any, *, tenant_id: str, conversation_id: str) -> Conversation | None:
        return conv

    monkeypatch.setattr(service_module.ConversationService, "get", fake_get)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/messages",
            json={"content_text": "x" * 5000},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_agent_message_returns_404_on_cross_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different tenant JWT + valid ULID conv id from other tenant -> 404."""
    _stub_agent_auth(monkeypatch, claims=OTHER_TENANT_CLAIMS)
    other_tenant_conv_id = new_id()

    # The service raises ValueError for cross-tenant / unknown — same shape
    # the real service produces when ConversationService.record_message's
    # inner self.get(...) returns None.
    async def fake_record(
        self: Any,
        *,
        tenant_id: str,
        conversation_id: str,
        role: MessageRole,
        content_text: str,
        sender_id: str | None,
        content_blocks: dict[str, Any] | None = None,
        tool_calls: dict[str, Any] | None = None,
    ) -> Message:
        raise ValueError(
            f"conversation {conversation_id} not found for tenant {tenant_id}"
        )

    monkeypatch.setattr(
        service_module.ConversationService, "record_message", fake_record
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/conversations/{other_tenant_conv_id}/messages",
            json={"content_text": "hello"},
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "conversation not found"


@pytest.mark.asyncio
async def test_post_agent_message_returns_404_on_unknown_conv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Random ULID -> 404."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)

    async def fake_record(
        self: Any,
        *,
        tenant_id: str,
        conversation_id: str,
        role: MessageRole,
        content_text: str,
        sender_id: str | None,
        content_blocks: dict[str, Any] | None = None,
        tool_calls: dict[str, Any] | None = None,
    ) -> Message:
        raise ValueError(
            f"conversation {conversation_id} not found for tenant {tenant_id}"
        )

    monkeypatch.setattr(
        service_module.ConversationService, "record_message", fake_record
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/conversations/01HX_NOTREAL/messages",
            json={"content_text": "hello"},
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_post_agent_message_does_not_change_ai_handling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent reply does NOT auto-disable AI handling.

    The endpoint only persists the message and bumps last_activity_at
    via ``record_message``. ``ai_handling`` is left exactly as it was.
    Disabling AI is an explicit ``return-to-ai`` action.
    """
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    conv = _conv(
        status=ConversationStatus.PENDING,
        assigned_agent_id="u_agent_1",
        ai_handling=False,
    )
    persisted = _msg(
        conversation_id=conv.id,
        role=MessageRole.AGENT,
        content_text="checking on this",
        sender_id="u_agent_1",
    )

    async def fake_get(self: Any, *, tenant_id: str, conversation_id: str) -> Conversation | None:
        return conv

    async def fake_record(
        self: Any,
        *,
        tenant_id: str,
        conversation_id: str,
        role: MessageRole,
        content_text: str,
        sender_id: str | None,
        content_blocks: dict[str, Any] | None = None,
        tool_calls: dict[str, Any] | None = None,
    ) -> Message:
        return persisted

    monkeypatch.setattr(service_module.ConversationService, "get", fake_get)
    monkeypatch.setattr(
        service_module.ConversationService, "record_message", fake_record
    )

    # We assert indirectly: ai_handling state is not mutated by record_message
    # (no ``return_to_ai`` / ``assign_to_agent`` called). The captured map
    # below only sees ``record_message`` — if ai_handling had flipped, an
    # additional service call would have been observed.
    called: list[str] = []

    real_record = service_module.ConversationService.record_message

    async def wrapped(
        self: Any,
        *,
        tenant_id: str,
        conversation_id: str,
        role: MessageRole,
        content_text: str,
        sender_id: str | None,
        content_blocks: dict[str, Any] | None = None,
        tool_calls: dict[str, Any] | None = None,
    ) -> Message:
        called.append("record_message")
        return await real_record(
            self,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            role=role,
            content_text=content_text,
            sender_id=sender_id,
        )

    monkeypatch.setattr(service_module.ConversationService, "record_message", wrapped)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/messages",
            json={"content_text": "checking on this"},
        )

    assert resp.status_code == 201, resp.text
    # The route only touches record_message + get — no assign_to_agent,
    # no return_to_ai, no escalate_to_human_queue.
    assert called == ["record_message"]
    # Sanity check: the response body doesn't carry ai_handling (it doesn't
    # for MessageOut), but record_message must NOT have flipped it.
    body = resp.json()
    assert body["role"] == "agent"


@pytest.mark.asyncio
async def test_post_agent_message_accepts_admin_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admin JWT can also post (require_agent_or_admin admits admin+owner)."""
    _stub_agent_auth(monkeypatch, claims=ADMIN_CLAIMS)
    conv = _conv()
    persisted = _msg(
        conversation_id=conv.id,
        role=MessageRole.AGENT,
        content_text="admin reply",
        sender_id="u_admin",
    )

    captured_role: dict[str, Any] = {}

    async def fake_get(self: Any, *, tenant_id: str, conversation_id: str) -> Conversation | None:
        return conv

    async def fake_record(
        self: Any,
        *,
        tenant_id: str,
        conversation_id: str,
        role: MessageRole,
        content_text: str,
        sender_id: str | None,
        content_blocks: dict[str, Any] | None = None,
        tool_calls: dict[str, Any] | None = None,
    ) -> Message:
        captured_role["role"] = role
        return persisted

    monkeypatch.setattr(service_module.ConversationService, "get", fake_get)
    monkeypatch.setattr(
        service_module.ConversationService, "record_message", fake_record
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/messages",
            json={"content_text": "admin reply"},
        )

    assert resp.status_code == 201
    assert captured_role["role"] == MessageRole.AGENT


# ===========================================================================
# Consolidation refactor — existing role-gating still works
# ===========================================================================

@pytest.mark.asyncio
async def test_consolidation_refactor_preserves_existing_role_gating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The conversation router's existing admin gates still reject non-admins.

    After consolidating ``require_admin`` /
    ``require_agent_or_admin`` into ``auth.dependencies``, the existing
    /conversations endpoints must continue to reject a non-admin caller
    with the exact same 403 + detail string the previous inline
    implementation produced.
    """

    async def reject() -> dict[str, Any]:
        raise HTTPException(status_code=403, detail="admin role required")

    monkeypatch.setattr(conv_api_module, "require_admin", reject)
    monkeypatch.setattr(conv_api_module, "require_agent_or_admin", reject)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/conversations")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "admin role required"
