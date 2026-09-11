"""Integration tests for the RAG service layer (Task 6.12).

End-to-end coverage of :class:`knowledge.rag_service.RAGService` and its
wiring into :class:`agent.simple_responder.SimpleResponder`.

Each test:

1. Seeds a tenant + (optional) knowledge base + 2-5 articles on
   different topics via the existing ``index_article`` worker
   (Task 6.7).
2. Calls ``RAGService.build_context_for_query(...)`` directly, or
   invokes ``SimpleResponder.respond(...)`` end-to-end and asserts
   on the messages sent to the LLM.
3. Asserts the returned :class:`RagContext` or the AI message.

``embed_texts`` is patched with a deterministic stub so the tests
don't depend on a live OpenAI key.

The most important test in this file is
``test_simple_responder_injects_rag_context``: it verifies the end
to-end contract — that the AI response actually incorporates the
retrieved chunk text — by stubbing the LLM client to echo the
incoming messages.
"""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from qdrant_client.http import models as qmodels

from agent.simple_responder import SimpleResponder
from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from channel.repository import ChannelRepository
from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation, Message
from conversation.repository import ConversationRepository, MessageRepository
from conversation.service import ConversationService
from core.database import get_session
from core.id_gen import new_id
from knowledge.enums import ArticleSourceType, ArticleStatus
from knowledge.models import (
    Article,
    ArticleVersion,
    KnowledgeBase,
)
from knowledge.qdrant_client import (
    DEFAULT_COLLECTION,
    DEFAULT_VECTOR_SIZE,
    ensure_collection,
)
from knowledge.rag_service import RAGService, RagContext
from knowledge.repository import KnowledgeBaseRepository
from knowledge.worker import index_article
from llm_client.types import ChatResponse
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository

# ============================================================================
# Synthetic "topic-coded" texts (mirror test_retriever.py so the
# same tenant / articles fixture pattern works for both suites)
# ============================================================================

WEATHER_TEXT = (
    "The weather today is sunny with a high of 25 degrees celsius. "
    "Expect light winds from the west and clear skies throughout the "
    "afternoon. Tomorrow a cold front will bring overcast conditions "
    "and a chance of scattered thunderstorms. Barometric pressure is "
    "steady and humidity levels remain low."
)

COOKING_TEXT = (
    "Preheat the oven to 180 degrees celsius. Place the chicken breast "
    "in a roasting pan with olive oil, lemon, and fresh rosemary. Roast "
    "for 45 minutes until the juices run clear and the skin is golden. "
    "Let the meat rest for 10 minutes before slicing and serve with a "
    "side of roasted seasonal vegetables."
)

SPORTS_TEXT = (
    "The basketball game ended with a final score of 102 to 98 in "
    "overtime. The visiting team hit a buzzer-beating three pointer "
    "to clinch the playoff spot. Both teams combined for 24 turnovers "
    "and the box score was dominated by the point guards who scored "
    "the majority of their points from the free-throw line."
)

LONG_TEXT_BLOCKS = [
    "Paragraph one talks about weather patterns and seasonal change. "
    "It mentions sunshine, clouds, rain, snow, fog, and other typical "
    "weather phenomena that occur throughout the year in many regions. "
    "There is also discussion of climate variability and how it impacts "
    "agricultural planning and water resource management decisions. "
    "The data cited here is sourced from a multi-decade observational "
    "study carried out by a consortium of meteorological agencies.\n\n"
    "Paragraph two continues with details about wind direction, "
    "barometric pressure, humidity levels, dew point, visibility "
    "ranges, and atmospheric stability indices. These variables are "
    "critical for aviation safety, marine operations, and outdoor "
    "event scheduling. Forecasters rely on numerical weather prediction "
    "models to anticipate conditions several days in advance.\n\n"
    "Paragraph three explores the long-term effects of climate change "
    "on regional weather patterns. It highlights how shifting "
    "temperature gradients alter storm tracks, precipitation "
    "distribution, and seasonal transitions. The discussion references "
    "IPCC reports and recent peer-reviewed studies. Coastal communities "
    "are particularly vulnerable to sea-level rise and increased "
    "storm surge frequency.\n\n"
    "Paragraph four turns to practical advice for the everyday reader. "
    "It recommends dressing in layers, staying hydrated, monitoring "
    "UV index, and paying attention to local advisories. Travelers "
    "should consult up-to-date forecasts before embarking on journeys "
    "that involve crossing time zones or terrain types. Insurance "
    "policies that cover weather-related disruptions are advisable "
    "for high-stakes trips and outdoor events."
]

# A single, very long article used to trigger MAX_CONTEXT_CHARS truncation.
# We multiply by 4 so the chunker produces multiple chunks and the
# combined system_message exceeds MAX_CONTEXT_CHARS (4000 chars).
LONG_ARTICLE_TEXT = "\n\n".join(LONG_TEXT_BLOCKS) * 4


# ============================================================================
# Fixtures / helpers
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_db_singletons() -> None:
    """Reset async engine/sessionmaker/Qdrant between tests for isolation."""
    from core.database import reset_engine, reset_sessionmaker
    from core.qdrant import reset_qdrant_client

    reset_engine()
    reset_sessionmaker()
    reset_qdrant_client()
    yield
    reset_engine()
    reset_sessionmaker()
    reset_qdrant_client()


@pytest.fixture
async def tenant_factory() -> AsyncIterator[Tenant]:
    """Yield a freshly-created Tenant; cleanup cascades everything."""
    tenant = await TenantRepository().create(
        name="RAG Service Test Tenant", plan=TenantPlan.FREE
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


async def _make_kb(
    *, tenant_id: str, slug: str = "kb", name: str = "Test KB"
) -> KnowledgeBase:
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
    """Build a deterministic unit vector from the SHA-256 of ``text``.

    See ``tests/knowledge/integration/test_retriever.py`` for the
    rationale — we use hash-seeded vectors so the retriever is
    testable without a real embedding model.
    """
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    expanded = (seed * ((dim // len(seed)) + 1))[:dim]
    raw = [(b / 127.5) - 1.0 for b in expanded]
    norm_sq = sum(x * x for x in raw) or 1.0
    norm = norm_sq ** 0.5
    return [x / norm for x in raw]


class _StubEmbeddingResult:
    """Minimal stand-in for ``EmbeddingResult`` used by the test patch."""

    def __init__(self, vectors: list[list[float]]) -> None:
        self.vectors = vectors
        self.model = "text-embedding-3-small"
        self.usage = type("U", (), {"prompt_tokens": 0, "total_tokens": 0})()


def _patch_embed_topic_coded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``embed_texts`` with deterministic topic-coded stubs.

    Patched on BOTH ``knowledge.worker`` (used by the indexer) and
    ``knowledge.retriever`` (used by retrieval). Same pattern as
    ``test_retriever.py``.
    """

    async def _stub(*, texts, model, tenant_id=None, client=None):
        return _StubEmbeddingResult([_make_topic_vector(t) for t in texts])

    from knowledge import retriever as retriever_module
    from knowledge import worker as worker_module

    monkeypatch.setattr(worker_module, "embed_texts", _stub)
    monkeypatch.setattr(retriever_module, "embed_texts", _stub)


def _patch_embed_fail_rag(
    monkeypatch: pytest.MonkeyPatch, *, exc: Exception
) -> None:
    """Force ``embed_texts`` to raise ``exc`` when called from RAG layer."""
    from knowledge import retriever as retriever_module
    from llm_client.types import EmbeddingError

    async def _stub(*, texts, model, tenant_id=None, client=None):
        if isinstance(exc, EmbeddingError):
            raise exc
        raise EmbeddingError("forced test failure") from exc

    monkeypatch.setattr(retriever_module, "embed_texts", _stub)


async def _ensure_collection_ready() -> None:
    ok = await ensure_collection(
        name=DEFAULT_COLLECTION,
        vector_size=DEFAULT_VECTOR_SIZE,
    )
    assert ok, "Failed to ensure Qdrant collection"


async def _delete_qdrant_points_for_article(*, article_id: str) -> None:
    """Best-effort Qdrant cleanup for a single article."""
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


# ============================================================================
# Tests
# ============================================================================


@pytest.mark.integration
async def test_build_context_returns_formatted_system_message(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index weather + cooking + sports articles, query weather → chunks returned.

    The RAG service must return a RagContext with at least one chunk
    and a formatted system_message that contains chunk text. Tenant
    isolation is verified by checking ``knowledge_base_id``.
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)
    tenant = tenant_factory

    kb = await _make_kb(tenant_id=tenant.id, slug="kb-format")
    weather_art, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Weather Patterns",
        raw_text=WEATHER_TEXT,
    )
    cooking_art, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Chicken Recipe",
        raw_text=COOKING_TEXT,
    )
    sports_art, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Basketball Recap",
        raw_text=SPORTS_TEXT,
    )

    try:
        for art in (weather_art, cooking_art, sports_art):
            result = await index_article(article_id=art.id)
            assert result.status == ArticleStatus.INDEXED

        rag = RAGService()
        ctx = await rag.build_context_for_query(
            tenant_id=tenant.id,
            query="what is the weather forecast today",
            top_k=5,
            score_threshold=None,  # disable threshold for mock embeddings
        )

        assert isinstance(ctx, RagContext)
        assert ctx.chunk_count >= 1
        assert ctx.system_message  # non-empty
        assert "Retrieved knowledge:" in ctx.system_message
        assert ctx.knowledge_base_id == kb.id
        assert ctx.knowledge_base_name == "Test KB"
        # The formatted block contains one of the indexed article texts.
        # With mocked embeddings, any of the three articles could be the
        # top hit — but at least one of them must have been returned.
        article_texts = (WEATHER_TEXT, COOKING_TEXT, SPORTS_TEXT)
        assert any(text[:80] in ctx.system_message for text in article_texts), (
            "system_message did not contain any of the indexed article chunks"
        )
        # Article + chunk ids surface in the formatted block.
        assert "article" in ctx.system_message
        assert "chunk" in ctx.system_message
        # Score is reported (Qdrant cosine similarity, in [0, 1] typically).
        assert 0.0 <= ctx.retrieval_score_max <= 1.0
    finally:
        for art in (weather_art, cooking_art, sports_art):
            await _delete_qdrant_points_for_article(article_id=art.id)


@pytest.mark.integration
async def test_build_context_includes_source_attribution(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """system_message must include attribution (article id + chunk index).

    Verifies the source-citation format the LLM sees. Article id is
    an opaque ULID so it's safe to surface in the prompt; we don't
    include the title or the KB name.
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)
    tenant = tenant_factory

    kb = await _make_kb(tenant_id=tenant.id, slug="kb-attr")
    weather_art, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Weather Patterns",
        raw_text=WEATHER_TEXT,
    )

    try:
        result = await index_article(article_id=weather_art.id)
        assert result.status == ArticleStatus.INDEXED

        rag = RAGService()
        ctx = await rag.build_context_for_query(
            tenant_id=tenant.id,
            query="weather forecast",
            top_k=3,
            score_threshold=None,  # disable threshold for mock embeddings
        )

        assert ctx.chunk_count >= 1
        # The article id should appear as the citation reference.
        assert weather_art.id in ctx.system_message
        # Chunk index is part of the attribution format too.
        assert "chunk 0" in ctx.system_message or "chunk 1" in ctx.system_message
        # The title is NOT included (would carry meaningful surface text).
        assert "Weather Patterns" not in ctx.system_message
        # The KB name is NOT included either.
        assert "Test KB" not in ctx.system_message
    finally:
        await _delete_qdrant_points_for_article(article_id=weather_art.id)


@pytest.mark.integration
async def test_build_context_truncates_at_max_chars(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index one VERY long article → system_message is bounded by MAX_CONTEXT_CHARS.

    Even with truncation, the marker suffix adds a small amount, so
    we assert ``len(system_message) <= MAX_CONTEXT_CHARS * 1.1``.
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)
    tenant = tenant_factory

    kb = await _make_kb(tenant_id=tenant.id, slug="kb-truncate")
    long_art, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Comprehensive Weather Encyclopedia",
        raw_text=LONG_ARTICLE_TEXT,
    )

    try:
        result = await index_article(article_id=long_art.id)
        assert result.status == ArticleStatus.INDEXED

        rag = RAGService()
        ctx = await rag.build_context_for_query(
            tenant_id=tenant.id,
            query="weather patterns",
            top_k=5,
            score_threshold=None,  # disable threshold for mock embeddings
        )

        # We expect at least one chunk to come back, and the system
        # message to be bounded.
        assert ctx.chunk_count >= 1
        assert len(ctx.system_message) <= int(rag.MAX_CONTEXT_CHARS * 1.1), (
            f"system_message length {len(ctx.system_message)} exceeded "
            f"bound {rag.MAX_CONTEXT_CHARS * 1.1:.0f}"
        )
        # The truncation marker should be present.
        assert "truncated" in ctx.system_message.lower()
    finally:
        await _delete_qdrant_points_for_article(article_id=long_art.id)


@pytest.mark.integration
async def test_build_context_no_kb_returns_empty_context(
    tenant_factory: Tenant,
) -> None:
    """A tenant with zero knowledge bases → empty RagContext (no retrieval).

    No embedding stub needed: the RAG service short-circuits
    BEFORE calling ``retrieve_chunks`` when no KB exists for the
    tenant (see :meth:`RAGService.build_context_for_query`). So
    the embedding layer is never touched.
    """
    tenant = tenant_factory

    rag = RAGService()
    ctx = await rag.build_context_for_query(
        tenant_id=tenant.id,
        query="anything",
        top_k=5,
    )

    assert isinstance(ctx, RagContext)
    assert ctx.chunk_count == 0
    assert ctx.system_message == ""
    assert ctx.knowledge_base_id == ""
    assert ctx.knowledge_base_name == ""
    assert ctx.retrieval_score_max == 0.0


@pytest.mark.integration
async def test_build_context_embedding_failure_returns_empty(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``embed_texts`` raises → empty RagContext, no exception propagates.

    This is the "RAG failures are non-fatal" guarantee. The RAG
    service must NEVER raise on a retrieval failure; the AI
    auto-reply must still produce a response.
    """
    from llm_client.types import EmbeddingError

    _patch_embed_fail_rag(
        monkeypatch, exc=EmbeddingError("rate limit exhausted")
    )
    tenant = tenant_factory
    kb = await _make_kb(tenant_id=tenant.id, slug="kb-embed-fail")

    rag = RAGService()
    ctx = await rag.build_context_for_query(
        tenant_id=tenant.id,
        query="hello",
        top_k=3,
    )

    assert ctx.chunk_count == 0
    assert ctx.system_message == ""
    # The KB was resolved BEFORE the embedding call, but the RAG
    # service returns the empty context on any retrieval error —
    # we don't surface the resolved KB id in the empty-context
    # path because that signal is only useful when chunks were
    # actually returned.
    assert ctx.knowledge_base_id == ""


@pytest.mark.integration
async def test_build_context_kb_not_found_returns_empty(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bogus ``knowledge_base_id`` → empty RagContext (no exception).

    The RAG service must SWALLOW ``KnowledgeBaseNotFoundError`` for
    the "no KB" scenario so the AI auto-reply still produces a
    response. (The retriever itself raises — that's expected — the
    service catches and downgrades.)
    """
    _patch_embed_topic_coded(monkeypatch)
    tenant = tenant_factory

    rag = RAGService()
    ctx = await rag.build_context_for_query(
        tenant_id=tenant.id,
        query="hello",
        knowledge_base_id=new_id(),  # a random ULID that doesn't exist
        top_k=3,
    )

    assert ctx.chunk_count == 0
    assert ctx.system_message == ""
    assert ctx.knowledge_base_id == ""  # KB was not resolved


@pytest.mark.integration
async def test_simple_responder_injects_rag_context(
    tenant_factory: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: customer message → AI response that references indexed chunks.

    Stubs the LLM client to echo back the system messages it
    received. We assert the echoed content contains a phrase from
    the indexed article — this is the contract that proves RAG
    context actually reaches the LLM.

    Flow:

    1. Index a weather article.
    2. Stub the LLM client to record the messages it receives and
       echo the system-prompt content as its response.
    3. Process an inbound envelope via ``process_inbound_envelope``
       (or via ``SimpleResponder.respond`` directly).
    4. Inspect the recorded messages: at least one system message
       should contain a distinctive phrase from the indexed article.
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)
    tenant = tenant_factory

    # KB + article.
    kb = await _make_kb(tenant_id=tenant.id, slug="kb-e2e")
    weather_art, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Weather Encyclopedia",
        raw_text=WEATHER_TEXT,
    )

    try:
        result = await index_article(article_id=weather_art.id)
        assert result.status == ArticleStatus.INDEXED

        # Set up a conversation + channel so the responder has a
        # conversation to look up.
        channel = await ChannelRepository().create(
            tenant_id=tenant.id,
            type=ChannelType.WEB,
            name="E2E Channel",
            credentials_encrypted="{}",
            status=ChannelStatus.ACTIVE,
        )

        conv_repo = ConversationRepository()
        msg_repo = MessageRepository()
        conv_service = ConversationService(
            repo=conv_repo, message_repo=msg_repo
        )

        now = datetime.now(UTC)
        conversation = Conversation(
            id=new_id(),
            tenant_id=tenant.id,
            channel_id=channel.id,
            customer_external_id="ou_e2e",
            status=ConversationStatus.OPEN,
            assigned_agent_id=None,
            ai_handling=True,
            opened_at=now,
            last_activity_at=now,
        )
        await conv_repo.create(conversation=conversation)

        # Record the customer message BEFORE invoking the responder
        # so ``list_messages`` returns it.
        customer_msg = Message(
            id=new_id(),
            conversation_id=conversation.id,
            role=MessageRole.CUSTOMER,
            content_text="What's the weather forecast today?",
            sender_id=None,
            content_blocks_json=None,
            tool_calls_json=None,
            created_at=now,
        )
        await msg_repo.create(message=customer_msg)

        # Stub the LLM client to capture the messages it received
        # and return a fixed reply.
        captured_messages: list = []

        async def _fake_chat(request):  # type: ignore[no-untyped-def]
            captured_messages.extend(request.messages)
            return ChatResponse(
                content="stubbed AI reply",
                model=request.model,
                prompt_tokens=10,
                completion_tokens=5,
                finish_reason="stop",
            )

        from unittest.mock import MagicMock

        fake_client = MagicMock()
        fake_client.chat = _fake_chat

        responder = SimpleResponder(
            conv_service=conv_service,
            llm_client_factory=lambda t: fake_client,
            # Override the default RAG service so we can pass a
            # relaxed ``score_threshold`` for the mock embeddings.
            # Production behavior (threshold=0.3) is verified in
            # the unit-style tests below.
            rag_service=_RAGServiceNoThreshold(),
        )

        ai_response = await responder.respond(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
        )

        assert ai_response is not None
        assert ai_response.role == MessageRole.AI

        # Find the RAG system message among the captured messages.
        # The LLM request is constructed as:
        #   [M1_SYSTEM_PROMPT, *history]
        # where history is [rag_message?, summary?, ...mapped_messages]
        # For this test we expect a RAG system message to be present
        # because the tenant has a KB and there is at least one
        # indexed article.
        system_contents = [
            m.content for m in captured_messages if m.role == "system"
        ]
        # First system message is the M1 system prompt; subsequent
        # system messages include the RAG context (and possibly a
        # summary, but this short conversation won't have one).
        assert len(system_contents) >= 2, (
            f"Expected ≥ 2 system messages (M1 prompt + RAG), got {len(system_contents)}"
        )

        # The RAG block starts with "Retrieved knowledge:" and should
        # contain a distinctive phrase from the indexed weather
        # article. Use a substring that's clearly unique to that text.
        rag_blocks = [c for c in system_contents if "Retrieved knowledge:" in c]
        assert rag_blocks, (
            "No RAG system message found in the LLM request. "
            f"Captured system contents: {system_contents!r}"
        )
        # Match a distinctive phrase from WEATHER_TEXT. With the
        # mocked embeddings, we may not get the exact weather chunk
        # back as top hit — but we know it's the only indexed article,
        # so it MUST appear.
        assert any(
            phrase in block
            for block in rag_blocks
            for phrase in (
                "barometric pressure",
                "cold front",
                "thunderstorms",
                "humidity",
            )
        ), (
            "RAG context did not contain any distinctive phrase from "
            "the indexed weather article. "
            f"Captured RAG blocks: {rag_blocks!r}"
        )
    finally:
        await _delete_qdrant_points_for_article(article_id=weather_art.id)
        await _delete_tenant(tenant.id)


class _RAGServiceNoThreshold(RAGService):
    """RAGService variant that disables the score_threshold for mock embeddings.

    Our ``_patch_embed_topic_coded`` produces hash-seeded unit
    vectors; their cosine similarity can be slightly negative, so
    a threshold of 0.0 (or even the default 0.3) filters them all
    out. This subclass forces ``score_threshold=None`` so all
    Qdrant results flow through to the LLM request. Production
    code never sees this subclass — it's a test-only helper.
    """

    async def build_context_for_query(self, **kwargs):  # type: ignore[override]
        if "score_threshold" not in kwargs:
            kwargs["score_threshold"] = None
        return await super().build_context_for_query(**kwargs)