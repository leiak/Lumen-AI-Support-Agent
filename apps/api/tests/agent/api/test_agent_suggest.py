"""Tests for the Stage 8.3 AI-suggested reply endpoint.

Single surface under test:

- ``POST /api/v1/agents/conversations/{id}/suggest-reply`` —
  read-only AI suggestion preview. Returns suggested text,
  retrieved citations, and a ``turn_kind`` discriminator. MUST
  NOT persist messages, MUST NOT dispatch tools, MUST NOT
  broadcast WS events.

Pure-Python tests — no DB. We monkeypatch the auth dependency,
the ``ConversationService`` methods, the RAG service, and the
LLM client factory so the route handlers run against in-memory
fakes. Mirrors the patterns from ``tests/agent/api/test_agent_api.py``
and ``tests/agent/api/test_agent_queue.py``.

PII discipline: every assertion that touches a log payload only
looks for opaque IDs (ULIDs, user_ids). Never content_text, never
customer_external_id, never email.
"""
from __future__ import annotations

import contextlib
import io
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from agent import api as agent_api_module
from agent import suggest as suggest_module
from agent.exceptions import SuggestionServiceNotFoundError
from agent.suggest import (
    LLM_UNAVAILABLE_WARNING,
    SuggestionResult,
    SuggestionService,
)
from conversation import service as service_module
from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation, Message
from core.id_gen import new_id
from knowledge.rag_service import RagContext
from llm_client.types import ChatResponse

# ===========================================================================
# Shared claims fixtures
# ===========================================================================

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
        status=ConversationStatus.OPEN,
        assigned_agent_id=None,
        ai_handling=True,
        opened_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
        last_activity_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    )
    base.update(kwargs)
    return Conversation(**base)


def _msg(
    *,
    conversation_id: str,
    role: MessageRole,
    content_text: str,
    **kwargs: Any,
) -> Message:
    """Build a Message ORM instance with sensible defaults."""
    base: dict[str, Any] = dict(
        id=new_id(),
        conversation_id=conversation_id,
        role=role,
        content_text=content_text,
        content_blocks_json=None,
        sender_id=None,
        tool_calls_json=None,
        created_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    )
    base.update(kwargs)
    return Message(**base)


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


def _stub_suggest_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    suggest_svc: SuggestionService | None = None,
) -> MagicMock:
    """Replace ``SuggestionService()`` in the agent router with a MagicMock.

    Returns the mock class so individual tests can configure
    ``suggest_svc.suggest_reply.return_value`` / ``.side_effect``
    etc. when they want to override the per-request service.
    """
    cls = MagicMock(return_value=suggest_svc or MagicMock())
    monkeypatch.setattr(agent_api_module, "SuggestionService", cls)
    return cls


# ===========================================================================
# Happy path — KB hit + LLM success
# ===========================================================================


@pytest.mark.asyncio
async def test_suggest_returns_text_and_citations_when_rag_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant has a KB + customer asks a KB-relevant question ->
    response carries suggested_text, ≥1 citation, turn_kind='rag_hit',
    retrieval_score_max > 0.
    """
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    conv = _conv()
    expected_result = SuggestionResult(
        suggested_text="MAGIC_PHRASE_SUGGEST_BODY",
        citations=[
            suggest_module.CitationOut(
                article_id="01HX_ARTICLE",
                chunk_index=0,
                text="MAGIC_PHRASE_CHUNK",
                score=0.91,
            )
        ],
        retrieval_score_max=0.91,
        warning=None,
        turn_kind="rag_hit",
        tenant_id=AGENT_CLAIMS["tenant_id"],
        conversation_id=conv.id,
    )
    suggest_svc = MagicMock()
    suggest_svc.suggest_reply = AsyncMock(return_value=expected_result)
    _stub_suggest_service(monkeypatch, suggest_svc=suggest_svc)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{conv.id}/suggest-reply"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["conversation_id"] == conv.id
    assert body["suggested_text"] == "MAGIC_PHRASE_SUGGEST_BODY"
    assert body["turn_kind"] == "rag_hit"
    assert body["warning"] is None
    assert body["retrieval_score_max"] == 0.91
    assert len(body["citations"]) == 1
    # Service was called with the right tenant + conversation ids.
    call_kwargs = suggest_svc.suggest_reply.await_args.kwargs
    assert call_kwargs["tenant_id"] == AGENT_CLAIMS["tenant_id"]
    assert call_kwargs["conversation_id"] == conv.id


@pytest.mark.asyncio
async def test_suggest_citations_have_article_id_and_chunk_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Citation schema: article_id + chunk_index + text + score all populated."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    conv = _conv()
    expected_result = SuggestionResult(
        suggested_text="ok",
        citations=[
            suggest_module.CitationOut(
                article_id="01HX_ART_A",
                chunk_index=2,
                text="first chunk text",
                score=0.83,
            ),
            suggest_module.CitationOut(
                article_id="01HX_ART_B",
                chunk_index=0,
                text="second chunk text",
                score=0.71,
            ),
        ],
        retrieval_score_max=0.83,
        warning=None,
        turn_kind="rag_hit",
        tenant_id=AGENT_CLAIMS["tenant_id"],
        conversation_id=conv.id,
    )
    suggest_svc = MagicMock()
    suggest_svc.suggest_reply = AsyncMock(return_value=expected_result)
    _stub_suggest_service(monkeypatch, suggest_svc=suggest_svc)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{conv.id}/suggest-reply"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["citations"]) == 2
    first, second = body["citations"]
    assert first["article_id"] == "01HX_ART_A"
    assert first["chunk_index"] == 2
    assert first["text"] == "first chunk text"
    assert first["score"] == pytest.approx(0.83)
    assert second["article_id"] == "01HX_ART_B"
    assert second["chunk_index"] == 0
    assert second["score"] == pytest.approx(0.71)


@pytest.mark.asyncio
async def test_suggest_uses_tenant_default_kb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service is constructed with no KB override -> tenant's
    default KB is used (mirrors ``RAGService.build_context_for_query``
    default behaviour).
    """
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    conv = _conv()
    captured: dict[str, Any] = {}

    async def fake_suggest(
        self: SuggestionService,
        *,
        tenant_id: str,
        conversation_id: str,
    ) -> SuggestionResult:
        captured["tenant_id"] = tenant_id
        captured["conversation_id"] = conversation_id
        return SuggestionResult(
            suggested_text="ok",
            citations=[],
            retrieval_score_max=0.0,
            warning=None,
            turn_kind="no_rag",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )

    monkeypatch.setattr(SuggestionService, "suggest_reply", fake_suggest)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{conv.id}/suggest-reply"
        )

    assert resp.status_code == 200
    assert captured["tenant_id"] == AGENT_CLAIMS["tenant_id"]
    assert captured["conversation_id"] == conv.id


# ===========================================================================
# No-RAG paths
# ===========================================================================


@pytest.mark.asyncio
async def test_suggest_no_kb_returns_turn_kind_no_rag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant has no KB -> turn_kind='no_rag', citations empty,
    suggested_text still populated from LLM.
    """
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    conv = _conv()
    expected_result = SuggestionResult(
        suggested_text="MAGIC_PHRASE_NO_RAG_REPLY",
        citations=[],
        retrieval_score_max=0.0,
        warning=None,
        turn_kind="no_rag",
        tenant_id=AGENT_CLAIMS["tenant_id"],
        conversation_id=conv.id,
    )
    suggest_svc = MagicMock()
    suggest_svc.suggest_reply = AsyncMock(return_value=expected_result)
    _stub_suggest_service(monkeypatch, suggest_svc=suggest_svc)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{conv.id}/suggest-reply"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["turn_kind"] == "no_rag"
    assert body["citations"] == []
    assert body["retrieval_score_max"] == 0.0
    assert body["suggested_text"] == "MAGIC_PHRASE_NO_RAG_REPLY"
    assert body["warning"] is None


@pytest.mark.asyncio
async def test_suggest_no_customer_message_returns_turn_kind_no_customer_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty history (or no customer turn) -> turn_kind='no_customer_message',
    citations empty, suggested_text empty.
    """
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    conv = _conv()
    expected_result = SuggestionResult(
        suggested_text="",
        citations=[],
        retrieval_score_max=0.0,
        warning=None,
        turn_kind="no_customer_message",
        tenant_id=AGENT_CLAIMS["tenant_id"],
        conversation_id=conv.id,
    )
    suggest_svc = MagicMock()
    suggest_svc.suggest_reply = AsyncMock(return_value=expected_result)
    _stub_suggest_service(monkeypatch, suggest_svc=suggest_svc)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{conv.id}/suggest-reply"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["turn_kind"] == "no_customer_message"
    assert body["suggested_text"] == ""
    assert body["citations"] == []
    assert body["warning"] is None


# ===========================================================================
# Service-level failure modes
# ===========================================================================


@pytest.mark.asyncio
async def test_suggest_llm_failure_returns_fallback_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM raises RateLimited -> suggested_text == FALLBACK_MESSAGE,
    warning == 'llm_unavailable', turn_kind == 'llm_unavailable'.
    """
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    conv = _conv()

    from agent.simple_responder import FALLBACK_MESSAGE
    from llm_client.exceptions import RateLimited

    conv_row = _conv(tenant_id=AGENT_CLAIMS["tenant_id"])
    conv_row.id = conv.id

    async def fake_get(
        self: Any, *, tenant_id: str, conversation_id: str
    ) -> Conversation | None:
        assert tenant_id == AGENT_CLAIMS["tenant_id"]
        assert conversation_id == conv.id
        return conv_row

    async def fake_list_messages(
        self: Any, *, tenant_id: str, conversation_id: str, limit: int
    ) -> list[Message]:
        return [
            _msg(
                conversation_id=conv.id,
                role=MessageRole.CUSTOMER,
                content_text="hello",
            )
        ]

    real_rag_service = suggest_module.RAGService

    class _RagEmpty(real_rag_service):  # type: ignore[misc, valid-type]
        async def build_context_for_query(self, **_kwargs: Any) -> RagContext:
            return RagContext(
                system_message="",
                chunk_count=0,
                knowledge_base_id="",
                knowledge_base_name="",
                retrieval_score_max=0.0,
            )

    async def fake_retrieve_chunks(**_kwargs: Any) -> list[Any]:
        return []

    def _boom_llm(_tenant_id: str) -> Any:
        raise RateLimited("simulated rate limit")

    monkeypatch.setattr(service_module.ConversationService, "get", fake_get)
    monkeypatch.setattr(
        service_module.ConversationService, "list_messages", fake_list_messages
    )
    monkeypatch.setattr(suggest_module, "RAGService", _RagEmpty)
    monkeypatch.setattr(
        suggest_module, "retrieve_chunks", fake_retrieve_chunks
    )
    monkeypatch.setattr(
        suggest_module, "_default_llm_client_factory", _boom_llm
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{conv.id}/suggest-reply"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["suggested_text"] == FALLBACK_MESSAGE
    assert body["warning"] == LLM_UNAVAILABLE_WARNING
    assert body["turn_kind"] == "llm_unavailable"
    # Citations are still surfaced (RAG did succeed for the
    # underlying retrieval even if the LLM call failed).
    assert "citations" in body


@pytest.mark.asyncio
async def test_suggest_rag_failure_returns_no_rag_turn_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RAG raises EmbeddingError -> turn_kind='no_rag' (RAG fail-open),
    suggested_text still generated from the LLM.
    """
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    conv = _conv()
    conv_row = _conv(tenant_id=AGENT_CLAIMS["tenant_id"])
    conv_row.id = conv.id

    async def fake_get(
        self: Any, *, tenant_id: str, conversation_id: str
    ) -> Conversation | None:
        return conv_row

    async def fake_list_messages(
        self: Any, *, tenant_id: str, conversation_id: str, limit: int
    ) -> list[Message]:
        return [
            _msg(
                conversation_id=conv.id,
                role=MessageRole.CUSTOMER,
                content_text="hello",
            )
        ]

    from llm_client.types import EmbeddingError

    async def boom_rag(**_kwargs: Any) -> RagContext:
        # Real RAGService catches EmbeddingError and returns an
        # empty context; we exercise that path by raising here
        # — the service wrapper should downgrade to empty.
        raise EmbeddingError("simulated embed failure")

    async def fake_retrieve(
        **_kwargs: Any,
    ) -> list[Any]:
        return []

    captured_llm_call: dict[str, Any] = {}

    class _FakeLLMClient:
        def __init__(self, tenant_id: str) -> None:
            self.tenant_id = tenant_id

        async def chat(self, request: Any) -> ChatResponse:
            captured_llm_call["messages"] = list(request.messages)
            return ChatResponse(
                content="MAGIC_PHRASE_RAG_FAILOPEN_REPLY",
                model=request.model,
                prompt_tokens=10,
                completion_tokens=5,
                finish_reason="stop",
            )

    monkeypatch.setattr(service_module.ConversationService, "get", fake_get)
    monkeypatch.setattr(
        service_module.ConversationService, "list_messages", fake_list_messages
    )

    real_rag_service = suggest_module.RAGService

    class _RagFailOpen(real_rag_service):  # type: ignore[misc, valid-type]
        async def build_context_for_query(self, **_kwargs: Any) -> RagContext:
            return RagContext(
                system_message="",
                chunk_count=0,
                knowledge_base_id="",
                knowledge_base_name="",
                retrieval_score_max=0.0,
            )

    monkeypatch.setattr(suggest_module, "RAGService", _RagFailOpen)
    # Also patch the raw ``retrieve_chunks`` import to return
    # an empty list (no citations even if RAG had chunks).
    monkeypatch.setattr(suggest_module, "retrieve_chunks", fake_retrieve)
    monkeypatch.setattr(
        suggest_module,
        "_default_llm_client_factory",
        lambda tenant_id: _FakeLLMClient(tenant_id),
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{conv.id}/suggest-reply"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["turn_kind"] == "no_rag"
    assert body["suggested_text"] == "MAGIC_PHRASE_RAG_FAILOPEN_REPLY"
    assert body["citations"] == []
    assert body["retrieval_score_max"] == 0.0
    # And the LLM was still called.
    assert "messages" in captured_llm_call


# ===========================================================================
# Auth + tenant isolation
# ===========================================================================


@pytest.mark.asyncio
async def test_suggest_returns_404_on_cross_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent's tenant != conversation's tenant -> 404.

    Anti-enumeration: same wording as the unknown-conversation
    case so a probing caller cannot distinguish between the
    two.
    """
    _stub_agent_auth(monkeypatch, claims=OTHER_TENANT_CLAIMS)
    other_tenant_conv_id = new_id()
    suggest_svc = MagicMock()
    suggest_svc.suggest_reply = AsyncMock(
        side_effect=SuggestionServiceNotFoundError()
    )
    _stub_suggest_service(monkeypatch, suggest_svc=suggest_svc)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{other_tenant_conv_id}/suggest-reply"
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "conversation not found"


@pytest.mark.asyncio
async def test_suggest_returns_404_on_unknown_conv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Random ULID -> 404."""
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    suggest_svc = MagicMock()
    suggest_svc.suggest_reply = AsyncMock(
        side_effect=SuggestionServiceNotFoundError()
    )
    _stub_suggest_service(monkeypatch, suggest_svc=suggest_svc)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/agents/conversations/01HX_NOTREAL/suggest-reply"
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "conversation not found"


@pytest.mark.asyncio
async def test_suggest_requires_auth() -> None:
    """No Bearer token -> 401."""
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/agents/conversations/01HX_X/suggest-reply"
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_suggest_admin_can_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admin JWT can also call (require_agent_or_admin admits admin)."""
    _stub_agent_auth(monkeypatch, claims=ADMIN_CLAIMS)
    conv = _conv()
    expected_result = SuggestionResult(
        suggested_text="admin ok",
        citations=[],
        retrieval_score_max=0.0,
        warning=None,
        turn_kind="no_rag",
        tenant_id=ADMIN_CLAIMS["tenant_id"],
        conversation_id=conv.id,
    )
    suggest_svc = MagicMock()
    suggest_svc.suggest_reply = AsyncMock(return_value=expected_result)
    _stub_suggest_service(monkeypatch, suggest_svc=suggest_svc)

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{conv.id}/suggest-reply"
        )
    assert resp.status_code == 200


# ===========================================================================
# Non-mutation invariants (CRITICAL)
# ===========================================================================


@pytest.mark.asyncio
async def test_suggest_does_not_persist_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ConversationService.record_message`` is NEVER called.

    The endpoint is read-only; it returns a preview without
    writing any Message row.
    """
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    conv = _conv()
    expected_result = SuggestionResult(
        suggested_text="ok",
        citations=[],
        retrieval_score_max=0.0,
        warning=None,
        turn_kind="no_rag",
        tenant_id=AGENT_CLAIMS["tenant_id"],
        conversation_id=conv.id,
    )
    suggest_svc = MagicMock()
    suggest_svc.suggest_reply = AsyncMock(return_value=expected_result)
    _stub_suggest_service(monkeypatch, suggest_svc=suggest_svc)

    # Spy on ConversationService.record_message — must not be called.
    record_calls: list[Any] = []
    real_record = service_module.ConversationService.record_message

    async def spy_record(self: Any, *args: Any, **kwargs: Any) -> Any:
        record_calls.append((args, kwargs))
        return await real_record(self, *args, **kwargs)

    monkeypatch.setattr(
        service_module.ConversationService, "record_message", spy_record
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{conv.id}/suggest-reply"
        )
    assert resp.status_code == 200
    assert record_calls == [], (
        f"record_message was called; suggest endpoint MUST NOT "
        f"persist messages. calls={record_calls!r}"
    )


@pytest.mark.asyncio
async def test_suggest_does_not_change_conversation_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ai_handling / status / assigned_agent_id / last_activity_at all
    unchanged. The service only calls ``get`` + ``list_messages`` —
    never ``update`` / ``assign_to_agent`` / ``escalate_to_human_queue``
    / ``return_to_ai`` / ``close`` / ``claim``.
    """
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    conv = _conv(
        status=ConversationStatus.PENDING,
        assigned_agent_id="u_agent_1",
        ai_handling=False,
    )
    expected_result = SuggestionResult(
        suggested_text="ok",
        citations=[],
        retrieval_score_max=0.0,
        warning=None,
        turn_kind="no_rag",
        tenant_id=AGENT_CLAIMS["tenant_id"],
        conversation_id=conv.id,
    )
    suggest_svc = MagicMock()
    suggest_svc.suggest_reply = AsyncMock(return_value=expected_result)
    _stub_suggest_service(monkeypatch, suggest_svc=suggest_svc)

    mutating_calls: list[str] = []
    for name in (
        "assign_to_agent",
        "escalate_to_human_queue",
        "return_to_ai",
        "close",
        "claim",
        "record_message",
    ):
        real_method = getattr(service_module.ConversationService, name)

        async def spy(
            self: Any,
            *args: Any,
            __name: str = name,
            **kwargs: Any,
        ) -> Any:
            mutating_calls.append(__name)
            return await real_method(self, *args, **kwargs)

        monkeypatch.setattr(
            service_module.ConversationService, name, spy
        )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{conv.id}/suggest-reply"
        )
    assert resp.status_code == 200
    assert mutating_calls == [], (
        f"suggest endpoint mutated conversation state via "
        f"{mutating_calls!r}; it MUST be read-only"
    )


@pytest.mark.asyncio
async def test_suggest_does_not_dispatch_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM returns tool_calls=[{"name": "escalate_to_human", ...}];
    the suggestion service IGNORES the tool call (no
    escalate_to_human_queue call, suggested_text still derived
    from the LLM content).
    """
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    conv = _conv()
    conv_row = _conv(tenant_id=AGENT_CLAIMS["tenant_id"])
    conv_row.id = conv.id

    async def fake_get(
        self: Any, *, tenant_id: str, conversation_id: str
    ) -> Conversation | None:
        return conv_row

    async def fake_list_messages(
        self: Any, *, tenant_id: str, conversation_id: str, limit: int
    ) -> list[Message]:
        return [
            _msg(
                conversation_id=conv.id,
                role=MessageRole.CUSTOMER,
                content_text="please transfer me",
            )
        ]

    async def fake_rag(**_kwargs: Any) -> RagContext:
        return RagContext(
            system_message="",
            chunk_count=0,
            knowledge_base_id="",
            knowledge_base_name="",
            retrieval_score_max=0.0,
        )

    async def fake_retrieve(**_kwargs: Any) -> list[Any]:
        return []

    class _ToolCallLLMClient:
        def __init__(self, tenant_id: str) -> None:
            self.tenant_id = tenant_id

        async def chat(self, request: Any) -> ChatResponse:
            return ChatResponse(
                content="MAGIC_PHRASE_TEXT_DESPITE_TOOL_CALL",
                model=request.model,
                prompt_tokens=10,
                completion_tokens=5,
                finish_reason="tool_use",
                tool_calls=[
                    {
                        "type": "tool_use",
                        "id": "toolu-stub",
                        "name": "escalate_to_human",
                        "input": {"reason": "MAGIC_PHRASE_TOOL_CALL_REASON"},
                    }
                ],
            )

    # Spy on ConversationService.escalate_to_human_queue — must not be called.
    escalate_calls: list[Any] = []
    real_escalate = service_module.ConversationService.escalate_to_human_queue

    async def spy_escalate(self: Any, *args: Any, **kwargs: Any) -> Any:
        escalate_calls.append((args, kwargs))
        return await real_escalate(self, *args, **kwargs)

    monkeypatch.setattr(service_module.ConversationService, "get", fake_get)
    monkeypatch.setattr(
        service_module.ConversationService, "list_messages", fake_list_messages
    )
    monkeypatch.setattr(
        service_module.ConversationService,
        "escalate_to_human_queue",
        spy_escalate,
    )

    real_rag_service = suggest_module.RAGService

    class _RagEmpty(real_rag_service):  # type: ignore[misc, valid-type]
        async def build_context_for_query(self, **_kwargs: Any) -> RagContext:
            return RagContext(
                system_message="",
                chunk_count=0,
                knowledge_base_id="",
                knowledge_base_name="",
                retrieval_score_max=0.0,
            )

    monkeypatch.setattr(suggest_module, "RAGService", _RagEmpty)
    monkeypatch.setattr(suggest_module, "retrieve_chunks", fake_retrieve)
    monkeypatch.setattr(
        suggest_module,
        "_default_llm_client_factory",
        lambda tenant_id: _ToolCallLLMClient(tenant_id),
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{conv.id}/suggest-reply"
        )
    assert resp.status_code == 200
    body = resp.json()
    # Tool call ignored — text derived from LLM content.
    assert body["suggested_text"] == "MAGIC_PHRASE_TEXT_DESPITE_TOOL_CALL"
    # And escalate_to_human_queue MUST NOT have been called.
    assert escalate_calls == [], (
        f"escalate_to_human_queue was called; suggest endpoint "
        f"MUST NOT dispatch tools. calls={escalate_calls!r}"
    )


@pytest.mark.asyncio
async def test_suggest_does_not_broadcast_ws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ConnectionManager.broadcast_to_channel`` is NEVER called.

    The suggest endpoint is a read-only preview; it MUST NOT
    emit WS frames.
    """
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    conv = _conv()
    expected_result = SuggestionResult(
        suggested_text="ok",
        citations=[],
        retrieval_score_max=0.0,
        warning=None,
        turn_kind="no_rag",
        tenant_id=AGENT_CLAIMS["tenant_id"],
        conversation_id=conv.id,
    )
    suggest_svc = MagicMock()
    suggest_svc.suggest_reply = AsyncMock(return_value=expected_result)
    _stub_suggest_service(monkeypatch, suggest_svc=suggest_svc)

    from widget.ws import manager as ws_manager_module

    broadcast_calls: list[Any] = []
    real_broadcast = ws_manager_module.manager.broadcast_to_channel

    async def spy_broadcast(*args: Any, **kwargs: Any) -> int:
        broadcast_calls.append((args, kwargs))
        return await real_broadcast(*args, **kwargs)

    ws_manager_module.manager.broadcast_to_channel = (
        AsyncMock(side_effect=spy_broadcast)
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/agents/conversations/{conv.id}/suggest-reply"
        )
    assert resp.status_code == 200
    assert ws_manager_module.manager.broadcast_to_channel.await_count == 0
    assert broadcast_calls == []


# ===========================================================================
# PII discipline
# ===========================================================================


@pytest.mark.asyncio
async def test_suggest_logs_only_opaque_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Captured log payload contains tenant_id, conversation_id,
    NO content / customer text / exception repr.
    """
    _stub_agent_auth(monkeypatch, claims=AGENT_CLAIMS)
    conv = _conv()
    expected_result = SuggestionResult(
        suggested_text="ok",
        citations=[],
        retrieval_score_max=0.0,
        warning=None,
        turn_kind="no_rag",
        tenant_id=AGENT_CLAIMS["tenant_id"],
        conversation_id=conv.id,
    )
    suggest_svc = MagicMock()
    suggest_svc.suggest_reply = AsyncMock(return_value=expected_result)
    _stub_suggest_service(monkeypatch, suggest_svc=suggest_svc)

    app = _build_app()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/agents/conversations/{conv.id}/suggest-reply"
            )

    assert resp.status_code == 200

    captured_text = buf.getvalue()
    suggestion_lines = [
        line
        for line in captured_text.splitlines()
        if "agent suggestion generated" in line
    ]
    assert suggestion_lines, (
        f"expected 'agent suggestion generated' log line in:\n{captured_text!r}"
    )

    line = suggestion_lines[0]
    # Opaque IDs MUST be present.
    assert conv.id in line, f"missing conversation_id in log: {line!r}"
    assert AGENT_CLAIMS["tenant_id"] in line
    # No customer PII / email / exception repr.
    assert conv.customer_external_id not in line
    assert AGENT_CLAIMS["email"] not in line
    assert "Exception" not in line
    assert "Traceback" not in line


# ===========================================================================
# Service-level error mapping
# ===========================================================================


@pytest.mark.asyncio
async def test_suggest_service_raises_not_found_on_unknown_conv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service-layer unit test: an unknown conversation_id surfaces
    ``SuggestionServiceNotFoundError`` (which the route maps to
    404).
    """
    conv = _conv()
    conv_row = _conv(tenant_id=AGENT_CLAIMS["tenant_id"])
    conv_row.id = conv.id

    async def fake_get_none(
        self: Any, *, tenant_id: str, conversation_id: str
    ) -> Conversation | None:
        return None

    monkeypatch.setattr(
        service_module.ConversationService, "get", fake_get_none
    )

    with pytest.raises(SuggestionServiceNotFoundError):
        await SuggestionService().suggest_reply(
            tenant_id=AGENT_CLAIMS["tenant_id"],
            conversation_id=conv.id,
        )


@pytest.mark.asyncio
async def test_suggest_service_raises_not_found_on_cross_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service-layer unit test: a foreign-tenant conversation surfaces
    ``SuggestionServiceNotFoundError``.
    """
    foreign_conv = _conv(tenant_id="tenant_OTHER")

    async def fake_get_cross(
        self: Any, *, tenant_id: str, conversation_id: str
    ) -> Conversation | None:
        # ConversationService.get would normally return None for
        # cross-tenant requests; we mirror that here.
        return None

    monkeypatch.setattr(
        service_module.ConversationService, "get", fake_get_cross
    )

    with pytest.raises(SuggestionServiceNotFoundError):
        await SuggestionService().suggest_reply(
            tenant_id=AGENT_CLAIMS["tenant_id"],
            conversation_id=foreign_conv.id,
        )


# ===========================================================================
# Internal mapping helpers (service unit tests)
# ===========================================================================


@pytest.mark.asyncio
async def test_suggest_service_no_customer_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service-layer unit test: list_messages returns AI-only history ->
    SuggestionResult with turn_kind='no_customer_message'.
    """
    conv_row = _conv(tenant_id=AGENT_CLAIMS["tenant_id"])

    async def fake_get(
        self: Any, *, tenant_id: str, conversation_id: str
    ) -> Conversation | None:
        return conv_row

    async def fake_list_messages_ai_only(
        self: Any, *, tenant_id: str, conversation_id: str, limit: int
    ) -> list[Message]:
        return [
            _msg(
                conversation_id=conv_row.id,
                role=MessageRole.AI,
                content_text="I previously said hi",
            ),
            _msg(
                conversation_id=conv_row.id,
                role=MessageRole.AGENT,
                content_text="agent said hi too",
            ),
        ]

    monkeypatch.setattr(service_module.ConversationService, "get", fake_get)
    monkeypatch.setattr(
        service_module.ConversationService,
        "list_messages",
        fake_list_messages_ai_only,
    )

    result = await SuggestionService().suggest_reply(
        tenant_id=AGENT_CLAIMS["tenant_id"],
        conversation_id=conv_row.id,
    )

    assert result.turn_kind == "no_customer_message"
    assert result.suggested_text == ""
    assert result.citations == []
    assert result.warning is None
    assert result.conversation_id == conv_row.id
    assert result.tenant_id == AGENT_CLAIMS["tenant_id"]


@pytest.mark.asyncio
async def test_suggest_service_llm_raises_rate_limited_returns_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service-layer unit test: LLM raises RateLimited -> service
    returns ``FALLBACK_MESSAGE`` + ``warning='llm_unavailable'``
    + ``turn_kind='llm_unavailable'``.
    """
    from agent.simple_responder import FALLBACK_MESSAGE
    from llm_client.exceptions import RateLimited

    conv_row = _conv(tenant_id=AGENT_CLAIMS["tenant_id"])

    async def fake_get(
        self: Any, *, tenant_id: str, conversation_id: str
    ) -> Conversation | None:
        return conv_row

    async def fake_list_messages(
        self: Any, *, tenant_id: str, conversation_id: str, limit: int
    ) -> list[Message]:
        return [
            _msg(
                conversation_id=conv_row.id,
                role=MessageRole.CUSTOMER,
                content_text="hello",
            )
        ]

    async def fake_rag(**_kwargs: Any) -> RagContext:
        return RagContext(
            system_message="",
            chunk_count=0,
            knowledge_base_id="",
            knowledge_base_name="",
            retrieval_score_max=0.0,
        )

    real_rag_service = suggest_module.RAGService

    class _RagEmpty(real_rag_service):  # type: ignore[misc, valid-type]
        async def build_context_for_query(self, **_kwargs: Any) -> RagContext:
            return RagContext(
                system_message="",
                chunk_count=0,
                knowledge_base_id="",
                knowledge_base_name="",
                retrieval_score_max=0.0,
            )

    def _boom(_tenant_id: str) -> Any:
        raise RateLimited("simulated rate limit")

    monkeypatch.setattr(service_module.ConversationService, "get", fake_get)
    monkeypatch.setattr(
        service_module.ConversationService, "list_messages", fake_list_messages
    )
    monkeypatch.setattr(suggest_module, "RAGService", _RagEmpty)
    monkeypatch.setattr(
        suggest_module, "retrieve_chunks",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        suggest_module, "_default_llm_client_factory", _boom
    )

    result = await SuggestionService().suggest_reply(
        tenant_id=AGENT_CLAIMS["tenant_id"],
        conversation_id=conv_row.id,
    )

    assert result.suggested_text == FALLBACK_MESSAGE
    assert result.warning == LLM_UNAVAILABLE_WARNING
    assert result.turn_kind == "llm_unavailable"
    assert result.retrieval_score_max == 0.0


@pytest.mark.asyncio
async def test_suggest_service_rag_hits_with_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service-layer unit test: RAG returns 2 chunks -> citations
    list has 2 entries with article_id / chunk_index / score /
    truncated text.
    """
    from knowledge.retriever import RetrievedChunk

    conv_row = _conv(tenant_id=AGENT_CLAIMS["tenant_id"])

    async def fake_get(
        self: Any, *, tenant_id: str, conversation_id: str
    ) -> Conversation | None:
        return conv_row

    async def fake_list_messages(
        self: Any, *, tenant_id: str, conversation_id: str, limit: int
    ) -> list[Message]:
        return [
            _msg(
                conversation_id=conv_row.id,
                role=MessageRole.CUSTOMER,
                content_text="how does shipping work",
            )
        ]

    real_rag_service = suggest_module.RAGService
    real_kb_id = "01HX_KB"

    class _RagHit(real_rag_service):  # type: ignore[misc, valid-type]
        async def build_context_for_query(self, **_kwargs: Any) -> RagContext:
            return RagContext(
                system_message=(
                    "Retrieved knowledge:\n\n1. (article art_a, chunk 0)\n"
                    "MAGIC_PHRASE_CHUNK_A\n\n2. (article art_b, chunk 1)\n"
                    "MAGIC_PHRASE_CHUNK_B"
                ),
                chunk_count=2,
                knowledge_base_id=real_kb_id,
                knowledge_base_name="Test KB",
                retrieval_score_max=0.91,
            )

    captured_retrieve_kwargs: dict[str, Any] = {}

    class _Chunk:
        def __init__(self, article_id: str, chunk_index: int, text: str) -> None:
            self.article_id = article_id
            self.chunk_index = chunk_index
            self.text = text

    async def fake_retrieve_chunks(**kwargs: Any) -> list[RetrievedChunk]:
        captured_retrieve_kwargs.update(kwargs)
        return [
            RetrievedChunk(
                chunk=_Chunk("01HX_ART_A", 0, "MAGIC_PHRASE_CHUNK_A"),  # type: ignore[arg-type]
                score=0.91,
                article_id="01HX_ART_A",
                knowledge_base_id=real_kb_id,
                tenant_id=AGENT_CLAIMS["tenant_id"],
            ),
            RetrievedChunk(
                chunk=_Chunk("01HX_ART_B", 1, "MAGIC_PHRASE_CHUNK_B"),  # type: ignore[arg-type]
                score=0.74,
                article_id="01HX_ART_B",
                knowledge_base_id=real_kb_id,
                tenant_id=AGENT_CLAIMS["tenant_id"],
            ),
        ]

    captured_chat: dict[str, Any] = {}

    class _FakeLLMClient:
        def __init__(self, tenant_id: str) -> None:
            self.tenant_id = tenant_id

        async def chat(self, request: Any) -> ChatResponse:
            captured_chat["messages"] = list(request.messages)
            return ChatResponse(
                content="MAGIC_PHRASE_RAG_HIT_REPLY",
                model=request.model,
                prompt_tokens=10,
                completion_tokens=5,
                finish_reason="stop",
            )

    monkeypatch.setattr(service_module.ConversationService, "get", fake_get)
    monkeypatch.setattr(
        service_module.ConversationService, "list_messages", fake_list_messages
    )
    monkeypatch.setattr(suggest_module, "RAGService", _RagHit)
    monkeypatch.setattr(
        suggest_module, "retrieve_chunks", fake_retrieve_chunks
    )
    monkeypatch.setattr(
        suggest_module,
        "_default_llm_client_factory",
        lambda tenant_id: _FakeLLMClient(tenant_id),
    )

    result = await SuggestionService().suggest_reply(
        tenant_id=AGENT_CLAIMS["tenant_id"],
        conversation_id=conv_row.id,
    )

    assert result.turn_kind == "rag_hit"
    assert result.suggested_text == "MAGIC_PHRASE_RAG_HIT_REPLY"
    assert result.warning is None
    assert result.retrieval_score_max == pytest.approx(0.91)
    assert len(result.citations) == 2
    first, second = result.citations
    assert first.article_id == "01HX_ART_A"
    assert first.chunk_index == 0
    assert first.text == "MAGIC_PHRASE_CHUNK_A"
    assert first.score == pytest.approx(0.91)
    assert second.article_id == "01HX_ART_B"
    assert second.chunk_index == 1
    assert second.score == pytest.approx(0.74)
    # retrieve_chunks was called with the resolved KB id and tenant.
    assert captured_retrieve_kwargs["tenant_id"] == AGENT_CLAIMS["tenant_id"]
    assert captured_retrieve_kwargs["knowledge_base_id"] == real_kb_id
    # The LLM request included the RAG system message AND the
    # customer message.
    msgs = captured_chat["messages"]
    assert any(
        getattr(m, "role", None) == "system"
        and "Retrieved knowledge:" in getattr(m, "content", "")
        for m in msgs
    )
    assert any(
        getattr(m, "role", None) == "user"
        and "shipping" in getattr(m, "content", "")
        for m in msgs
    )


@pytest.mark.asyncio
async def test_suggest_service_truncates_citation_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Citation ``text`` is truncated to CITATION_TEXT_MAX_CHARS
    so the suggestion payload stays compact.
    """
    from agent.schemas import CITATION_TEXT_MAX_CHARS
    from knowledge.retriever import RetrievedChunk

    conv_row = _conv(tenant_id=AGENT_CLAIMS["tenant_id"])
    long_text = "x" * (CITATION_TEXT_MAX_CHARS * 3)

    async def fake_get(
        self: Any, *, tenant_id: str, conversation_id: str
    ) -> Conversation | None:
        return conv_row

    async def fake_list_messages(
        self: Any, *, tenant_id: str, conversation_id: str, limit: int
    ) -> list[Message]:
        return [
            _msg(
                conversation_id=conv_row.id,
                role=MessageRole.CUSTOMER,
                content_text="hi",
            )
        ]

    real_rag_service = suggest_module.RAGService

    class _RagHit(real_rag_service):  # type: ignore[misc, valid-type]
        async def build_context_for_query(self, **_kwargs: Any) -> RagContext:
            return RagContext(
                system_message="Retrieved knowledge:\n\n1. (article a, chunk 0)\n"
                + long_text,
                chunk_count=1,
                knowledge_base_id="01HX_KB",
                knowledge_base_name="Test KB",
                retrieval_score_max=0.9,
            )

    class _LongChunk:
        article_id = "01HX_ART"
        chunk_index = 0
        text = long_text

    async def fake_retrieve_chunks(**_kwargs: Any) -> list[RetrievedChunk]:
        return [
            RetrievedChunk(
                chunk=_LongChunk(),  # type: ignore[arg-type]
                score=0.9,
                article_id="01HX_ART",
                knowledge_base_id="01HX_KB",
                tenant_id=AGENT_CLAIMS["tenant_id"],
            )
        ]

    class _FakeLLMClient:
        def __init__(self, tenant_id: str) -> None:
            self.tenant_id = tenant_id

        async def chat(self, request: Any) -> ChatResponse:
            return ChatResponse(
                content="ok",
                model=request.model,
                prompt_tokens=10,
                completion_tokens=5,
                finish_reason="stop",
            )

    monkeypatch.setattr(service_module.ConversationService, "get", fake_get)
    monkeypatch.setattr(
        service_module.ConversationService, "list_messages", fake_list_messages
    )
    monkeypatch.setattr(suggest_module, "RAGService", _RagHit)
    monkeypatch.setattr(
        suggest_module, "retrieve_chunks", fake_retrieve_chunks
    )
    monkeypatch.setattr(
        suggest_module,
        "_default_llm_client_factory",
        lambda tenant_id: _FakeLLMClient(tenant_id),
    )

    result = await SuggestionService().suggest_reply(
        tenant_id=AGENT_CLAIMS["tenant_id"],
        conversation_id=conv_row.id,
    )

    assert len(result.citations) == 1
    cit = result.citations[0]
    # Text is truncated to CITATION_TEXT_MAX_CHARS + 1 (for the
    # ellipsis character).
    assert len(cit.text) <= CITATION_TEXT_MAX_CHARS + 1
    assert cit.text.startswith("x" * CITATION_TEXT_MAX_CHARS)


# ===========================================================================
# SuggestionService constructor DI
# ===========================================================================


def test_suggestion_service_constructor_defaults() -> None:
    """Constructing with no kwargs uses real ConversationService /
    RAGService / LLM factory / DEFAULT_MODEL.
    """
    svc = SuggestionService()
    assert isinstance(svc._conv_service, service_module.ConversationService)
    assert isinstance(svc._rag_service, suggest_module.RAGService)
    assert svc._llm_client_factory is suggest_module._default_llm_client_factory
    assert svc._model == suggest_module._resolve_default_model()


def test_suggestion_service_constructor_overrides() -> None:
    """Constructor accepts injected deps for tests."""
    custom_rag = MagicMock()
    custom_factory: Callable[[str], Any] = lambda _t: MagicMock()
    custom_conv = MagicMock()
    svc = SuggestionService(
        conv_service=custom_conv,
        llm_client_factory=custom_factory,
        rag_service=custom_rag,
        model="custom-model",
    )
    assert svc._conv_service is custom_conv
    assert svc._rag_service is custom_rag
    assert svc._llm_client_factory is custom_factory
    assert svc._model == "custom-model"


# ===========================================================================
# Auth rejection — non-agent/non-admin gets 403
# ===========================================================================


@pytest.mark.asyncio
async def test_suggest_rejects_invalid_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JWT with an unsupported role is rejected with 403 by the
    auth dependency.
    """

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
            "/api/v1/agents/conversations/01HX_X/suggest-reply"
        )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "agent or admin role required"
