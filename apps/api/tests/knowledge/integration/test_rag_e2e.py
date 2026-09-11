"""End-to-end RAG integration tests (Task 6.13).

The "deep" integration layer for the RAG pipeline. Where Task 6.11
isolates the retriever and Task 6.12 covers ``RAGService`` +
``SimpleResponder`` in isolation, Task 6.13 exercises the FULL
E2E flow:

    customer message
        -> channel adapter ``parse_inbound``
        -> ``process_inbound_envelope``
        -> ``ConversationService.find_or_create_for_inbound``
        -> ``ConversationService.record_message`` (CUSTOMER row)
        -> ``SimpleResponder.respond``
            -> ``RAGService.build_context_for_query`` (Task 6.12)
                -> ``retrieve_chunks`` (Task 6.11)
                -> format + truncate
            -> inject synthetic system message into LLM history
            -> ``LLMClient.chat``
        -> ``ConversationService.record_message`` (AI row)
        -> ``_broadcast_ai_complete`` (widget WS only)

The tests stub the LLM client to capture the messages it received
and assert the captured payload contains the indexed KB content.
They also assert on persisted rows in the live DB.

Design decisions
----------------

* **Stub LLM only.** ``embed_texts`` is patched with deterministic
  hash-seeded unit vectors (same trick as
  ``test_rag_service.py`` / ``test_retriever.py``). The LLM client is
  stubbed via ``agent.simple_responder._default_llm_client_factory``
  so ``process_inbound_envelope`` (which constructs a fresh
  ``SimpleResponder()`` without arguments) picks up our fake.

* **Tenant isolation per test.** Every test seeds its own tenant +
  KB; cleanup cascades via ``Tenant.delete``.

* **Mock embedding threshold.** Our hash-seeded unit vectors have
  cosine similarities typically in ``[-0.1, 0.3]`` — under the
  default ``score_threshold=0.3`` every hit is dropped, which would
  short-circuit RAG and produce no chunks. We swap the default
  ``RAGService`` for a subclass that uses ``score_threshold=None``
  via ``monkeypatch.setattr(simple_responder_module, "RAGService",
  _NoThresholdRAGService)``. Production behavior is verified in the
  Task 6.12 tests with the default threshold.

* **WS broadcast.** The autouse fixture rebinds the
  ``ConnectionManager`` singleton used by both
  ``widget.ws.router`` and ``channel.inbound`` so the broadcast at
  the end of the pipeline targets the same connection table the
  tests (if any) registered on.

PII discipline
--------------

Articles use distinctive synthetic prose ("MAGIC_PHRASE_XYZ123") so
assertions are tight. We never log the chunk text or the
customer message verbatim.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from agent import simple_responder as simple_responder_module
from agent.simple_responder import RAGService, SimpleResponder
from channel.enums import ChannelStatus, ChannelType
from channel.inbound import process_inbound_envelope
from channel.models import Channel
from channel.repository import ChannelRepository
from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation, Message
from conversation.repository import ConversationRepository, MessageRepository
from core.database import get_session
from core.id_gen import new_id
from knowledge.enums import ArticleSourceType, ArticleStatus
from knowledge.models import Article, ArticleVersion, KnowledgeBase
from knowledge.qdrant_client import (
    DEFAULT_COLLECTION,
    DEFAULT_VECTOR_SIZE,
    ensure_collection,
)
from knowledge.rag_service import RagContext
from knowledge.worker import index_article
from llm_client.types import ChatResponse
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository
from widget.adapter import WebWidgetAdapter
from widget.ws.manager import ConnectionManager

# ============================================================================
# Synthetic "topic-coded" texts (with distinctive markers for assertions)
# ============================================================================

SHIPPING_TEXT = (
    "MAGIC_PHRASE_XYZ123. Our standard shipping policy takes 3 to 5 "
    "business days for domestic orders. International orders typically "
    "take 7 to 14 business days depending on the destination country "
    "and customs processing. Free shipping is available on all orders "
    "over 50 dollars. Tracking numbers are emailed within 24 hours "
    "of dispatch and can be tracked on the carrier's website. "
    "Expedited shipping options are available at checkout for an "
    "additional fee and reduce delivery time to 1 to 2 business days."
)

COOKING_TEXT = (
    "MAGIC_PHRASE_COOKING_789. Preheat the oven to 180 degrees celsius. "
    "Place the chicken breast in a roasting pan with olive oil, lemon, "
    "and fresh rosemary. Roast for 45 minutes until the juices run "
    "clear and the skin is golden. Let the meat rest for 10 minutes "
    "before slicing and serve with a side of roasted seasonal vegetables."
)

RETURN_POLICY_TEXT = (
    "MAGIC_PHRASE_RETURN_456. Items can be returned within 30 days of "
    "delivery for a full refund. The items must be unused and in their "
    "original packaging. Return shipping is free for defective items. "
    "Refunds are processed within 5 business days after we receive "
    "the returned item."
)


# ============================================================================
# Fixtures / helpers
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset DB + Qdrant singletons and rebind the WS manager.

    Same pattern as ``test_conversation_lifecycle.py`` and
    ``test_rag_service.py``. The WS rebinding is required because
    ``channel.inbound`` imports ``_wsm`` at module load; if the
    autouse fixture rebinds it on a fresh ``ConnectionManager``,
    the broadcast call at the end of ``process_inbound_envelope``
    targets the test's empty connection table rather than a
    cross-test singleton.
    """
    from core.database import reset_engine, reset_sessionmaker
    from core.qdrant import reset_qdrant_client

    import channel.inbound as inbound_module
    from widget.ws import manager as ws_manager_module
    from widget.ws import router as ws_router_module

    fresh = ConnectionManager()
    monkeypatch.setattr(ws_manager_module, "manager", fresh)
    monkeypatch.setattr(ws_router_module, "manager", fresh)
    monkeypatch.setattr(inbound_module, "_wsm", fresh)

    yield

    reset_engine()
    reset_sessionmaker()
    reset_qdrant_client()


@pytest.fixture
async def tenant_factory() -> AsyncIterator[Tenant]:
    """Yield a freshly-created Tenant; cleanup cascades everything."""
    tenant = await TenantRepository().create(
        name="RAG E2E Tenant", plan=TenantPlan.FREE
    )
    try:
        yield tenant
    finally:
        await _delete_tenant(tenant.id)


@pytest.fixture
async def second_tenant_factory() -> AsyncIterator[Tenant]:
    """Second tenant for cross-tenant isolation tests."""
    tenant = await TenantRepository().create(
        name="RAG E2E Tenant B", plan=TenantPlan.FREE
    )
    try:
        yield tenant
    finally:
        await _delete_tenant(tenant.id)


async def _delete_tenant(tenant_id: str) -> None:
    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t:
            await session.delete(t)
            await session.commit()


async def _make_channel(
    *, tenant_id: str, channel_type: ChannelType = ChannelType.WEB
) -> Channel:
    """Create an ACTIVE channel for the tenant."""
    return await ChannelRepository().create(
        tenant_id=tenant_id,
        type=channel_type,
        name="RAG E2E Channel",
        credentials_encrypted=json.dumps({}),
        status=ChannelStatus.ACTIVE,
    )


async def _make_kb(
    *, tenant_id: str, slug: str = "kb", name: str = "Test KB"
) -> KnowledgeBase:
    """Insert a KnowledgeBase row."""
    kb = KnowledgeBase(
        id=new_id(),
        tenant_id=tenant_id,
        name=name,
        slug=slug,
        description=None,
        embedding_model="text-embedding-3-small",
        chunk_size=800,
        chunk_overlap=100,
    )
    async with get_session() as session:
        session.add(kb)
        await session.commit()
        await session.refresh(kb)
    assert kb.id is not None
    return kb


async def _make_article_and_version(
    *,
    tenant_id: str,
    knowledge_base_id: str,
    title: str,
    raw_text: str,
) -> tuple[Article, ArticleVersion]:
    """Insert an Article + v1 ArticleVersion (no indexing)."""
    article = Article(
        id=new_id(),
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        title=title,
        source_uri=None,
        source_type=ArticleSourceType.MANUAL,
        status=ArticleStatus.DRAFT,
        current_version_id=None,
        error_message=None,
    )
    version = ArticleVersion(
        id=new_id(),
        article_id=article.id,
        version_number=1,
        raw_text=raw_text,
        content_hash=hashlib.sha256(raw_text.encode()).hexdigest(),
    )
    async with get_session() as session:
        session.add(article)
        await session.flush()
        session.add(version)
        await session.flush()
        article.current_version_id = version.id
        await session.commit()
        await session.refresh(article)
        await session.refresh(version)
    return article, version


# ============================================================================
# Mocking helpers — deterministic "topic-coded" embeddings
# ============================================================================


def _make_topic_vector(text: str, *, dim: int = DEFAULT_VECTOR_SIZE) -> list[float]:
    """Hash-seeded unit vector (same trick as test_retriever / test_rag_service)."""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    expanded = (seed * ((dim // len(seed)) + 1))[:dim]
    raw = [(b / 127.5) - 1.0 for b in expanded]
    norm_sq = sum(x * x for x in raw) or 1.0
    return [x / (norm_sq ** 0.5) for x in raw]


class _StubEmbeddingResult:
    """Minimal stand-in for ``EmbeddingResult``."""

    def __init__(self, vectors: list[list[float]]) -> None:
        self.vectors = vectors
        self.model = "text-embedding-3-small"
        self.usage = type("U", (), {"prompt_tokens": 0, "total_tokens": 0})()


def _patch_embed_topic_coded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``embed_texts`` on both the worker AND the retriever modules."""

    async def _stub(
        *, texts: list[str], model: str, tenant_id: str | None = None, client: Any = None
    ) -> _StubEmbeddingResult:
        return _StubEmbeddingResult([_make_topic_vector(t) for t in texts])

    from knowledge import retriever as retriever_module
    from knowledge import worker as worker_module

    monkeypatch.setattr(worker_module, "embed_texts", _stub)
    monkeypatch.setattr(retriever_module, "embed_texts", _stub)


def _patch_embed_fail_rag(
    monkeypatch: pytest.MonkeyPatch, *, exc: Exception
) -> None:
    """Force ``embed_texts`` to raise for the RAG retriever path only.

    The worker (which is called via ``index_article`` BEFORE the test
    pipeline runs) is NOT patched here, so the worker can still index
    articles into Qdrant. Only the retrieval-time embedding call
    raises, simulating an OpenAI failure during a customer turn.
    """
    from knowledge import retriever as retriever_module
    from llm_client.types import EmbeddingError

    async def _stub(
        *, texts: list[str], model: str, tenant_id: str | None = None, client: Any = None
    ) -> _StubEmbeddingResult:
        if isinstance(exc, EmbeddingError):
            raise exc
        raise EmbeddingError("forced test failure") from exc

    monkeypatch.setattr(retriever_module, "embed_texts", _stub)


class _NoThresholdRAGService(RAGService):
    """RAGService variant that disables the cosine-similarity threshold.

    Mock embeddings yield pairwise cosine similarities mostly below
    the production default of 0.3, so all chunks would be filtered
    out and the RAG path would always return empty. This subclass
    forces ``score_threshold=None`` so every Qdrant hit flows
    through to the LLM request.

    Only used in tests. Production code never imports this.
    """

    async def build_context_for_query(self, **kwargs: Any) -> RagContext:  # type: ignore[override]
        if "score_threshold" not in kwargs:
            kwargs["score_threshold"] = None
        return await super().build_context_for_query(**kwargs)


class _CapturingLLM:
    """Stub LLM client that records every call's ``messages`` list.

    Returns a fixed reply string, optionally varied per call so tests
    that fire multiple messages can verify which LLM call produced
    which AI reply.
    """

    def __init__(self, *, replies: list[str] | None = None) -> None:
        self.calls: list[list[Any]] = []
        self._replies = replies

    async def chat(self, request: Any, *, max_retries: int = 3) -> ChatResponse:
        self.calls.append(list(request.messages))
        if self._replies is not None and self.calls:
            content = self._replies[len(self.calls) - 1]
        else:
            content = f"stubbed AI reply #{len(self.calls)}"
        return ChatResponse(
            content=content,
            model=request.model,
            prompt_tokens=10,
            completion_tokens=5,
            finish_reason="stop",
        )


class _FailingChatLLM:
    """LLM client whose ``chat`` raises — used for the embedding-failure test.

    Even though the embedding failure short-circuits RAG before the
    LLM is called, the LLM still runs to produce the AI reply, so we
    want a working stub here.
    """

    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    async def chat(self, request: Any, *, max_retries: int = 3) -> ChatResponse:
        self.calls.append(list(request.messages))
        return ChatResponse(
            content="stubbed reply despite RAG failure",
            model=request.model,
            prompt_tokens=10,
            completion_tokens=5,
            finish_reason="stop",
        )


def _install_no_threshold_rag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``SimpleResponder()`` (no args) use a no-threshold RAG service."""
    monkeypatch.setattr(
        simple_responder_module, "RAGService", _NoThresholdRAGService
    )


def _install_stub_llm(
    monkeypatch: pytest.MonkeyPatch, *, llm: Any | None = None
) -> _CapturingLLM:
    """Install the stub LLM factory and return the capture object.

    The factory closes over ``llm`` so the same object sees every
    LLM call from ``SimpleResponder``. We patch on the
    ``simple_responder`` module symbol (which is what
    ``SimpleResponder.__init__`` reads via
    ``_default_llm_client_factory``).
    """
    if llm is None:
        llm = _CapturingLLM()
    monkeypatch.setattr(
        simple_responder_module,
        "_default_llm_client_factory",
        lambda _tenant_id: llm,
    )
    return llm if isinstance(llm, _CapturingLLM) else llm  # type: ignore[return-value]


async def _ensure_collection_ready() -> None:
    ok = await ensure_collection(
        name=DEFAULT_COLLECTION,
        vector_size=DEFAULT_VECTOR_SIZE,
    )
    assert ok, "Failed to ensure Qdrant collection"


async def _delete_qdrant_points_for_article(*, article_id: str) -> None:
    """Best-effort cleanup of Qdrant points for an article."""
    from qdrant_client.http import models as qmodels

    from core.qdrant import get_qdrant_client

    client = get_qdrant_client()
    flt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="article_id", match=qmodels.MatchValue(value=article_id)
            )
        ]
    )
    try:
        await client.delete(
            collection_name=DEFAULT_COLLECTION,
            points_selector=flt,
            wait=True,
        )
    except Exception:
        pass


async def _fire_envelope(
    *,
    tenant_id: str,
    channel: Channel,
    text: str,
    external_user_id: str = "ou_rag_e2e",
    external_conversation_id: str = "oc_rag_e2e",
    client_message_id: str | None = None,
) -> None:
    """Build a widget ``MessageEnvelope`` and run it through the inbound pipeline.

    Helper so the test bodies stay focused on assertions rather than
    plumbing. Uses the ``WebWidgetAdapter`` to mirror what the WS
    endpoint does for a ``{"type": "message", ...}`` frame.
    """
    adapter = WebWidgetAdapter()
    frame: dict[str, Any] = {
        "type": "message",
        "text": text,
        "external_user_id": external_user_id,
        "external_conversation_id": external_conversation_id,
    }
    if client_message_id is not None:
        frame["client_message_id"] = client_message_id
    envelope = await adapter.parse_inbound(raw=frame, channel=channel)
    await process_inbound_envelope(envelope)


# ============================================================================
# Test 1 — E2E RAG injection via process_inbound_envelope
# ============================================================================


@pytest.mark.integration
async def test_e2e_customer_question_about_kb_content_gets_ai_reply_with_context(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full E2E: customer asks about shipping → AI reply is generated and the
    captured LLM request contains a RAG context block with the shipping text.

    Pipeline:
        1. Index a shipping-policy article.
        2. Stub the LLM to capture messages.
        3. Fire a customer message via ``process_inbound_envelope``.
        4. Assert:
           - DB has 1 CUSTOMER row + 1 AI row on the new conversation.
           - The captured LLM request contains the distinctive phrase
             "MAGIC_PHRASE_XYZ123" inside a "Retrieved knowledge:" block.
           - The AI message text matches the stub's reply.
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)
    _install_no_threshold_rag(monkeypatch)
    llm = _install_stub_llm(monkeypatch)

    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id, slug="kb-shipping")
    shipping_art, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Shipping Policy",
        raw_text=SHIPPING_TEXT,
    )

    channel = await _make_channel(tenant_id=tenant.id)

    try:
        result = await index_article(article_id=shipping_art.id)
        assert result.status == ArticleStatus.INDEXED

        await _fire_envelope(
            tenant_id=tenant.id,
            channel=channel,
            text="how long does shipping take",
        )

        # ---- Assert on LLM calls ----
        # The responder may make one call (chat) or two (summary + chat) if
        # history exceeds MAX_HISTORY_BEFORE_SUMMARY. With one customer
        # message we expect exactly one chat call.
        assert len(llm.calls) >= 1, "expected at least one LLM call"
        chat_call = llm.calls[-1]  # the chat call (last one)
        system_contents = [m.content for m in chat_call if m.role == "system"]

        # M1 system prompt is always present.
        assert any("customer-service agent" in c for c in system_contents)
        # RAG block present (no-threshold RAG should yield at least one chunk).
        rag_blocks = [c for c in system_contents if "Retrieved knowledge:" in c]
        assert rag_blocks, (
            f"No RAG system message in LLM request; system contents: "
            f"{[c[:80] for c in system_contents]!r}"
        )
        # The shipping article's distinctive phrase MUST appear in the
        # RAG block — this is the contract under test.
        assert any("MAGIC_PHRASE_XYZ123" in block for block in rag_blocks), (
            f"RAG block did not contain the shipping text marker. "
            f"Captured RAG blocks: {rag_blocks!r}"
        )

        # ---- Assert on persisted rows ----
        conv = await ConversationRepository().find_open_by_channel_customer(
            channel_id=channel.id, customer_external_id="ou_rag_e2e"
        )
        assert conv is not None
        assert conv.ai_handling is True
        msgs = await MessageRepository().list_by_conversation(conversation_id=conv.id)
        roles = sorted(m.role for m in msgs)
        assert roles == sorted([MessageRole.CUSTOMER, MessageRole.AI]), roles
        customer_msgs = [m for m in msgs if m.role == MessageRole.CUSTOMER]
        ai_msgs = [m for m in msgs if m.role == MessageRole.AI]
        assert customer_msgs[0].content_text == "how long does shipping take"
        # The AI message text is the stub's reply.
        assert ai_msgs[0].content_text == "stubbed AI reply #1"
    finally:
        await _delete_qdrant_points_for_article(article_id=shipping_art.id)


# ============================================================================
# Test 2 — no KB → no RAG block in LLM request, AI reply still produced
# ============================================================================


@pytest.mark.integration
async def test_e2e_rag_skipped_when_no_kb(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant with zero KBs → RAG service short-circuits, AI reply still flows.

    Verifies the backward-compatibility invariant: a tenant with no
    knowledge base behaves identically to a no-RAG build. The
    captured LLM request MUST NOT contain a ``"Retrieved knowledge:"``
    system message, and the AI auto-reply MUST still be recorded.
    """
    _install_no_threshold_rag(monkeypatch)
    llm = _install_stub_llm(monkeypatch)

    tenant = tenant_factory
    channel = await _make_channel(tenant_id=tenant.id)

    try:
        await _fire_envelope(
            tenant_id=tenant.id,
            channel=channel,
            text="hello, do you have any support",
            external_user_id="ou_no_kb",
        )

        assert len(llm.calls) >= 1
        chat_call = llm.calls[-1]
        system_contents = [m.content for m in chat_call if m.role == "system"]
        # Only the M1 system prompt; no RAG block.
        assert not any("Retrieved knowledge:" in c for c in system_contents), (
            "RAG block leaked into LLM request despite tenant having no KB"
        )
        # M1 prompt still present.
        assert any("customer-service agent" in c for c in system_contents)

        # And the AI reply was recorded.
        conv = await ConversationRepository().find_open_by_channel_customer(
            channel_id=channel.id, customer_external_id="ou_no_kb"
        )
        assert conv is not None
        msgs = await MessageRepository().list_by_conversation(conversation_id=conv.id)
        ai_msgs = [m for m in msgs if m.role == MessageRole.AI]
        assert len(ai_msgs) == 1
        assert ai_msgs[0].content_text == "stubbed AI reply #1"
    finally:
        pass  # nothing to clean


# ============================================================================
# Test 3 — tenant isolation: tenant A only sees tenant A's KB
# ============================================================================


@pytest.mark.integration
async def test_e2e_rag_only_uses_tenants_own_kb(
    tenant_factory: Tenant,
    second_tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant A indexed a SHIPPING article; tenant B indexed a COOKING article.

    A customer message FROM TENANT A asking about shipping must
    surface shipping content, NOT cooking content. This is the
    cross-tenant isolation contract for the full E2E pipeline (not
    just the retriever in isolation).
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)
    _install_no_threshold_rag(monkeypatch)
    llm = _install_stub_llm(monkeypatch)

    tenant_a = tenant_factory
    tenant_b = second_tenant_factory

    kb_a = await _make_kb(tenant_id=tenant_a.id, slug="kb-a")
    kb_b = await _make_kb(tenant_id=tenant_b.id, slug="kb-b")

    shipping_art, _ = await _make_article_and_version(
        tenant_id=tenant_a.id,
        knowledge_base_id=kb_a.id,
        title="Shipping Policy",
        raw_text=SHIPPING_TEXT,
    )
    cooking_art, _ = await _make_article_and_version(
        tenant_id=tenant_b.id,
        knowledge_base_id=kb_b.id,
        title="Chicken Recipe",
        raw_text=COOKING_TEXT,
    )

    channel_a = await _make_channel(tenant_id=tenant_a.id)

    try:
        r_a = await index_article(article_id=shipping_art.id)
        r_b = await index_article(article_id=cooking_art.id)
        assert r_a.status == ArticleStatus.INDEXED
        assert r_b.status == ArticleStatus.INDEXED

        await _fire_envelope(
            tenant_id=tenant_a.id,
            channel=channel_a,
            text="tell me about shipping",
            external_user_id="ou_iso_a",
        )

        chat_call = llm.calls[-1]
        system_contents = [m.content for m in chat_call if m.role == "system"]
        rag_blocks = [c for c in system_contents if "Retrieved knowledge:" in c]
        assert rag_blocks, "RAG block missing for tenant A"
        rag_text = "\n".join(rag_blocks)
        # Shipping article MUST appear.
        assert "MAGIC_PHRASE_XYZ123" in rag_text, (
            f"Expected shipping marker in RAG block, got: {rag_text[:200]!r}"
        )
        # Cooking article MUST NOT appear (it's tenant B's content).
        assert "MAGIC_PHRASE_COOKING_789" not in rag_text, (
            "Cross-tenant leak: tenant B's cooking content surfaced in tenant A's RAG"
        )
    finally:
        await _delete_qdrant_points_for_article(article_id=shipping_art.id)
        await _delete_qdrant_points_for_article(article_id=cooking_art.id)


# ============================================================================
# Test 4 — multiple customer messages → multiple SimpleResponder calls
# ============================================================================


@pytest.mark.integration
async def test_e2e_multiple_messages_in_conversation_each_runs_rag(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 customer messages → 3 AI replies, each with its own RAG context.

    Verifies the responder runs RAG on every customer turn (not just
    the first). We index a SHIPPING article AND a RETURN article,
    fire one customer message per topic, and verify each LLM call's
    RAG block contains the topic's distinctive marker.
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)
    _install_no_threshold_rag(monkeypatch)

    # Three distinct replies so we can correlate each LLM call to a
    # customer turn by content.
    llm = _install_stub_llm(
        monkeypatch,
        llm=_CapturingLLM(
            replies=[
                "reply-1-shipping",
                "reply-2-return",
                "reply-3-shipping",
            ]
        ),
    )

    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id, slug="kb-multi-turn")
    shipping_art, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Shipping Policy",
        raw_text=SHIPPING_TEXT,
    )
    return_art, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Return Policy",
        raw_text=RETURN_POLICY_TEXT,
    )

    channel = await _make_channel(tenant_id=tenant.id)
    external_user = "ou_multi_turn"

    try:
        for art in (shipping_art, return_art):
            r = await index_article(article_id=art.id)
            assert r.status == ArticleStatus.INDEXED

        # Fire 3 customer messages.
        await _fire_envelope(
            tenant_id=tenant.id,
            channel=channel,
            text="how long does shipping take",
            external_user_id=external_user,
            client_message_id="cm_turn_1",
        )
        await _fire_envelope(
            tenant_id=tenant.id,
            channel=channel,
            text="what is your return policy",
            external_user_id=external_user,
            client_message_id="cm_turn_2",
        )
        await _fire_envelope(
            tenant_id=tenant.id,
            channel=channel,
            text="can I track my shipping order",
            external_user_id=external_user,
            client_message_id="cm_turn_3",
        )

        # 3 chat LLM calls (no summary call because total messages < threshold).
        chat_calls = llm.calls
        assert len(chat_calls) == 3, (
            f"expected 3 LLM calls, got {len(chat_calls)}"
        )

        # Each call must contain a RAG block.
        for i, call in enumerate(chat_calls):
            system_contents = [m.content for m in call if m.role == "system"]
            rag_blocks = [c for c in system_contents if "Retrieved knowledge:" in c]
            assert rag_blocks, (
                f"Call {i} (customer turn {i + 1}) missing RAG block. "
                f"System contents: {[c[:80] for c in system_contents]!r}"
            )

        # The first and third turns are shipping-themed; the second is
        # return-themed. The RAG block in each call should contain the
        # relevant article's marker. (Under mock embeddings we cannot
        # guarantee which chunk scores highest, but at least one chunk
        # per call must reference the article that is semantically
        # relevant to the query.)
        def _rag_text(call: list[Any]) -> str:
            return "\n".join(
                m.content
                for m in call
                if m.role == "system" and "Retrieved knowledge:" in m.content
            )

        call1_text = _rag_text(chat_calls[0])
        call2_text = _rag_text(chat_calls[1])
        call3_text = _rag_text(chat_calls[2])

        # Call 1 (shipping query) — must reference shipping marker.
        assert "MAGIC_PHRASE_XYZ123" in call1_text
        # Call 2 (return query) — must reference return marker.
        assert "MAGIC_PHRASE_RETURN_456" in call2_text
        # Call 3 (shipping query) — must reference shipping marker again.
        assert "MAGIC_PHRASE_XYZ123" in call3_text

        # ---- Persisted row count: 3 customer + 3 AI = 6 ----
        conv = await ConversationRepository().find_open_by_channel_customer(
            channel_id=channel.id, customer_external_id=external_user
        )
        assert conv is not None
        msgs = await MessageRepository().list_by_conversation(conversation_id=conv.id)
        roles = sorted(m.role for m in msgs)
        assert roles.count(MessageRole.CUSTOMER) == 3
        assert roles.count(MessageRole.AI) == 3
        # AI messages in chronological order match our stub replies.
        ai_msgs = [m for m in msgs if m.role == MessageRole.AI]
        ai_texts = [m.content_text for m in ai_msgs]
        assert ai_texts == [
            "reply-1-shipping",
            "reply-2-return",
            "reply-3-shipping",
        ], ai_texts
    finally:
        await _delete_qdrant_points_for_article(article_id=shipping_art.id)
        await _delete_qdrant_points_for_article(article_id=return_art.id)


# ============================================================================
# Test 5 — embedding failure does NOT block the AI reply
# ============================================================================


@pytest.mark.integration
async def test_e2e_rag_failure_does_not_block_ai_reply(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``embed_texts`` raises during retrieval → RAG returns empty, AI still responds.

    The embedding failure simulates an OpenAI outage at the moment
    the customer sends a message. We index an article first (with a
    working worker-side embedding stub), then break the
    retrieval-time embedding stub for the RAG call. The AI reply
    MUST still be generated — the response text is recorded and no
    RAG block appears in the captured LLM request.
    """
    await _ensure_collection_ready()

    # Index-time: worker uses a working stub.
    _patch_embed_topic_coded(monkeypatch)
    _install_no_threshold_rag(monkeypatch)
    llm = _install_stub_llm(
        monkeypatch, llm=_FailingChatLLM()
    )

    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id, slug="kb-embed-broken")
    shipping_art, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Shipping Policy",
        raw_text=SHIPPING_TEXT,
    )

    channel = await _make_channel(tenant_id=tenant.id)

    try:
        result = await index_article(article_id=shipping_art.id)
        assert result.status == ArticleStatus.INDEXED

        # Now break the retriever-side embedding so RAG fails during
        # the customer turn. This OVERRIDES the topic-coded stub on
        # the retriever module (the worker module is unaffected, so
        # we can still index articles beforehand).
        from llm_client.types import EmbeddingError

        _patch_embed_fail_rag(
            monkeypatch, exc=EmbeddingError("simulated OpenAI outage")
        )

        await _fire_envelope(
            tenant_id=tenant.id,
            channel=channel,
            text="how long does shipping take",
        )

        # LLM was still called despite the RAG failure.
        assert len(llm.calls) >= 1, (
            "AI reply was blocked by RAG failure — should have been "
            "generated with empty RAG context"
        )
        chat_call = llm.calls[-1]
        system_contents = [m.content for m in chat_call if m.role == "system"]
        # No RAG block — the graph's ``retrieve_node`` caught the
        # embedding failure and downgraded to an empty rag_messages
        # list, so the LLM request never received a RAG system
        # message.
        assert not any("Retrieved knowledge:" in c for c in system_contents), (
            f"RAG block leaked despite embedding failure: "
            f"{[c[:80] for c in system_contents]!r}"
        )
        # M1 prompt still present.
        assert any("customer-service agent" in c for c in system_contents)

        # AI reply was persisted with the stub's content.
        conv = await ConversationRepository().find_open_by_channel_customer(
            channel_id=channel.id, customer_external_id="ou_rag_e2e"
        )
        assert conv is not None
        msgs = await MessageRepository().list_by_conversation(conversation_id=conv.id)
        ai_msgs = [m for m in msgs if m.role == MessageRole.AI]
        assert len(ai_msgs) == 1
        assert ai_msgs[0].content_text == "stubbed reply despite RAG failure"
    finally:
        await _delete_qdrant_points_for_article(article_id=shipping_art.id)


# ============================================================================
# Test 6 — synthetic RAG system message is NOT persisted to messages table
# ============================================================================


@pytest.mark.integration
async def test_e2e_rag_does_not_persist_synthetic_system_message(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a successful RAG turn, only the customer + AI rows are persisted.

    The synthetic RAG system message lives ONLY in the LLM request
    payload — ``SimpleResponder._build_history`` returns it as a
    ``LLMChatMessage`` for the chat call but ``process_inbound_envelope``
    only persists CUSTOMER and AI messages via
    ``ConversationService.record_message``. A SYSTEM row in the
    messages table would be a leak of the LLM-prompt construction
    detail.
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)
    _install_no_threshold_rag(monkeypatch)
    llm = _install_stub_llm(monkeypatch)

    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id, slug="kb-no-persist")
    shipping_art, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Shipping Policy",
        raw_text=SHIPPING_TEXT,
    )

    channel = await _make_channel(tenant_id=tenant.id)

    try:
        result = await index_article(article_id=shipping_art.id)
        assert result.status == ArticleStatus.INDEXED

        await _fire_envelope(
            tenant_id=tenant.id,
            channel=channel,
            text="tell me about shipping",
        )

        # Sanity: the LLM request DID contain a RAG system message.
        # Otherwise the negative assertion below would be trivially
        # true and the test wouldn't prove what it claims.
        chat_call = llm.calls[-1]
        system_contents = [m.content for m in chat_call if m.role == "system"]
        rag_blocks = [c for c in system_contents if "Retrieved knowledge:" in c]
        assert rag_blocks, (
            "Test premise failed: RAG block missing from LLM request; "
            "the negative assertion below would be vacuous."
        )

        # Read the live DB and check that NO synthetic RAG system
        # message was persisted.
        conv = await ConversationRepository().find_open_by_channel_customer(
            channel_id=channel.id, customer_external_id="ou_rag_e2e"
        )
        assert conv is not None

        async with get_session() as session:
            stmt = select(Message).where(Message.conversation_id == conv.id)
            result = await session.execute(stmt)
            persisted: list[Message] = list(result.scalars().all())

        # Only the customer + AI rows.
        roles = sorted(m.role for m in persisted)
        assert roles == sorted([MessageRole.CUSTOMER, MessageRole.AI]), (
            f"Unexpected role set in messages table: {roles!r}. "
            f"A SYSTEM row would mean the synthetic RAG message leaked "
            f"into persistence."
        )
        assert len(persisted) == 2

        # And the persisted content is NOT the RAG context block.
        rag_texts = [m.content_text for m in persisted]
        assert not any("Retrieved knowledge:" in t for t in rag_texts), (
            f"Persisted messages contain 'Retrieved knowledge:' — the "
            f"synthetic RAG block leaked into the messages table. "
            f"Persisted contents: {rag_texts!r}"
        )
        # Magic phrase should not be persisted either.
        assert not any("MAGIC_PHRASE_XYZ123" in t for t in rag_texts), (
            "Magic phrase leaked into persisted messages — RAG content "
            "was persisted instead of staying in the LLM request."
        )
    finally:
        await _delete_qdrant_points_for_article(article_id=shipping_art.id)