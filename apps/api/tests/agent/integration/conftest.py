"""Fixtures for the Stage 7.5 agent integration tests.

Mirrors the inline pattern from
``tests/knowledge/integration/test_rag_e2e.py``. The agent tests
focus on the ``SimpleResponder.respond`` end-to-end path: real
Postgres + Qdrant + Redis infrastructure, with only the LLM stubbed
via the ``_default_llm_client_factory`` monkeypatch.

PII discipline: every article carries a distinctive
``MAGIC_PHRASE_*`` marker so assertions never need to read real
customer text. Tests assert against those markers only.

Stage 10 will introduce a shared ``conftest.py`` that hoists the
``_reset_db_singletons`` autouse fixture into one place; for now
this file localises the wiring to keep blast radius small.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from agent import simple_responder as simple_responder_module
from channel.enums import ChannelStatus, ChannelType
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
from knowledge.rag_service import RagContext, RAGService
from knowledge.worker import index_article
from llm_client.types import ChatResponse
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository

# ============================================================================
# Singleton reset (autouse)
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_db_singletons(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Reset the DB / Qdrant / Redis infrastructure singletons.

    The Stage 6 tech-debt item ("reset DB singletons between
    integration tests") is intentionally NOT centralised yet —
    Stage 10 will hoist this fixture into a shared ``conftest.py``
    and stop the per-suite duplication. For now we mirror what
    ``test_rag_e2e.py`` does inline.
    """
    from core.database import reset_engine, reset_sessionmaker
    from core.qdrant import reset_qdrant_client
    from core.redis import reset_redis

    yield

    reset_engine()
    reset_sessionmaker()
    reset_qdrant_client()
    reset_redis()


# ============================================================================
# Tenant + knowledge base + article fixture
# ============================================================================


async def _delete_tenant(tenant_id: str) -> None:
    """Cascade-delete a tenant (kills channels, conversations, messages)."""
    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t:
            await session.delete(t)
            await session.commit()


async def _make_channel(*, tenant_id: str) -> Channel:
    """Create an ACTIVE WEB channel for the tenant."""
    return await ChannelRepository().create(
        tenant_id=tenant_id,
        type=ChannelType.WEB,
        name="Agent E2E Channel",
        credentials_encrypted=json.dumps({}),
        status=ChannelStatus.ACTIVE,
    )


async def _make_kb(
    *,
    tenant_id: str,
    slug: str = "agent-e2e-kb",
    name: str = "Agent E2E KB",
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


def _make_topic_vector(text: str, *, dim: int = DEFAULT_VECTOR_SIZE) -> list[float]:
    """Hash-seeded unit vector (same trick as test_rag_e2e.py)."""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    expanded = (seed * ((dim // len(seed)) + 1))[:dim]
    raw = [(b / 127.5) - 1.0 for b in expanded]
    norm_sq = sum(x * x for x in raw) or 1.0
    return [x / (norm_sq ** 0.5) for x in raw]


def _patch_embed_topic_coded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``embed_texts`` on both the worker AND the retriever modules."""

    async def _stub(
        *,
        texts: list[str],
        model: str,
        tenant_id: str | None = None,
        client: Any = None,
    ) -> Any:
        vectors = [_make_topic_vector(t) for t in texts]

        class _Res:
            def __init__(self, vs: list[list[float]]) -> None:
                self.vectors = vs
                self.model = "text-embedding-3-small"
                self.usage = type("U", (), {"prompt_tokens": 0, "total_tokens": 0})()

        return _Res(vectors)

    from knowledge import retriever as retriever_module
    from knowledge import worker as worker_module

    monkeypatch.setattr(worker_module, "embed_texts", _stub)
    monkeypatch.setattr(retriever_module, "embed_texts", _stub)


@pytest.fixture
async def seeded_tenant_with_kb(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[Tenant, KnowledgeBase, Article, Channel]]:
    """Create tenant + KB + 1 article + run index_article against real Qdrant.

    Yields ``(tenant, kb, article, channel)``. Cleanup cascades via
    ``Tenant.delete``.
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)

    tenant = await TenantRepository().create(
        name="Agent E2E Tenant", plan=TenantPlan.FREE
    )
    kb = await _make_kb(tenant_id=tenant.id)
    article, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Shipping Policy",
        raw_text=_SHIPPING_TEXT,
    )
    channel = await _make_channel(tenant_id=tenant.id)
    try:
        result = await index_article(article_id=article.id)
        assert result.status == ArticleStatus.INDEXED
        yield tenant, kb, article, channel
    finally:
        await _delete_qdrant_points_for_article(article_id=article.id)
        await _delete_tenant(tenant.id)


@pytest.fixture
async def seeded_second_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[Tenant, KnowledgeBase, Article]]:
    """Second tenant + KB + article — for cross-tenant isolation tests.

    Cleanup cascades via ``Tenant.delete``.
    """
    await _ensure_collection_ready()
    _patch_embed_topic_coded(monkeypatch)

    tenant = await TenantRepository().create(
        name="Agent E2E Tenant B", plan=TenantPlan.FREE
    )
    kb = await _make_kb(tenant_id=tenant.id, slug="agent-e2e-kb-b")
    article, _ = await _make_article_and_version(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        title="Cooking Recipe",
        raw_text=_COOKING_TEXT,
    )
    try:
        result = await index_article(article_id=article.id)
        assert result.status == ArticleStatus.INDEXED
        yield tenant, kb, article
    finally:
        await _delete_qdrant_points_for_article(article_id=article.id)
        await _delete_tenant(tenant.id)


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
    except Exception:  # noqa: S110 — best-effort cleanup; failures here are not actionable
        pass


# ============================================================================
# Conversation + messages fixture (parameterized)
# ============================================================================


@pytest.fixture
def seeded_conv_with_messages() -> Callable[..., Any]:
    """Return an async factory that creates a tenant + channel + conversation
    + N messages, returning ``(tenant, channel, conv, cleanup)``.

    The factory returns a regular async function that returns a
    tuple of (tenant, channel, conv, cleanup_callable) — not an
    async generator — so the caller can ``await`` it directly and
    manage cleanup via its own try / finally.

    Cleanup cascades via ``Tenant.delete`` triggered by the
    returned ``cleanup_callable``.

    Usage::

        factory = seeded_conv_with_messages
        tenant, channel, conv, cleanup = await factory(n_messages=60)
        try:
            ...
        finally:
            await cleanup()
    """

    async def _factory(
        *,
        n_messages: int = 1,
        ai_handling: bool = True,
        tenant_name: str = "Agent E2E Conv Tenant",
        customer_external_id: str = "ou_agent_e2e",
    ) -> tuple[Tenant, Channel, Conversation, Callable[..., Any]]:
        tenant = await TenantRepository().create(
            name=tenant_name, plan=TenantPlan.FREE
        )
        channel = await _make_channel(tenant_id=tenant.id)
        repo = ConversationRepository()
        msg_repo = MessageRepository()
        now = datetime.now(UTC)
        conv = Conversation(
            id=new_id(),
            tenant_id=tenant.id,
            channel_id=channel.id,
            customer_external_id=customer_external_id,
            status=ConversationStatus.OPEN,
            assigned_agent_id=None,
            ai_handling=ai_handling,
            opened_at=now,
            last_activity_at=now,
        )
        await repo.create(conversation=conv)

        # Insert alternating customer / AI messages with distinctive
        # ``MAGIC_PHRASE_TURN_<i>`` markers so tests can assert that
        # specific old messages survive into the summary call.
        #
        # We bump ``created_at`` per row (1-ms increments) so the
        # repository's ``order by created_at desc`` returns a
        # deterministic, chronological order. Without per-row
        # timestamps the fixture relies on PostgreSQL's undefined
        # order for tied sort keys.
        base_ts = now
        for i in range(n_messages):
            role = MessageRole.CUSTOMER if i % 2 == 0 else MessageRole.AI
            text = f"MAGIC_PHRASE_TURN_{i} hello"
            ts = base_ts.replace(microsecond=(base_ts.microsecond + i) % 1_000_000)
            msg = Message(
                id=new_id(),
                conversation_id=conv.id,
                role=role,
                content_text=text,
                sender_id=None,
                content_blocks_json=None,
                tool_calls_json=None,
                created_at=ts,
            )
            await msg_repo.create(message=msg)

        async def _cleanup() -> None:
            await _delete_tenant(tenant.id)

        return tenant, channel, conv, _cleanup

    return _factory


# ============================================================================
# Stub LLM capture
# ============================================================================


class _CapturingLLM:
    """Stub LLM client that records every call's messages and returns
    configurable content + tool_calls.

    Mirrors the structure of the stub in ``test_rag_e2e.py`` but adds
    a configurable tool-call queue so tests can drive the
    escalation path without leaking that into the captured-call list.
    """

    def __init__(self) -> None:
        self.calls: list[list[Any]] = []
        self._text_replies: list[str | None] = [None]  # default text for call N
        self._tool_calls: list[list[dict[str, Any]] | None] = [None]
        self._raise_with: list[BaseException | None] = [None]

    def set_text(self, content: str) -> None:
        """Set the text reply for the next call."""
        self._text_replies.append(content)
        self._tool_calls.append(None)
        self._raise_with.append(None)

    def set_tool_call(self, *, name: str, args: dict[str, Any]) -> None:
        """Set a tool-call reply for the next call."""
        self._tool_calls.append(
            [
                {
                    "type": "tool_use",
                    "id": "toolu-stub",
                    "name": name,
                    "input": args,
                }
            ]
        )
        self._text_replies.append("")
        self._raise_with.append(None)

    def set_raises(self, exc: BaseException) -> None:
        """Make the next ``chat`` call raise the given exception."""
        self._raise_with.append(exc)
        self._text_replies.append(None)
        self._tool_calls.append(None)

    async def chat(
        self,
        request: Any,
        *,
        max_retries: int = 3,
    ) -> ChatResponse:
        idx = len(self.calls) + 1
        self.calls.append(list(request.messages))

        # Pop the queued response for this call. If the test did not
        # queue one, fall back to a generic stubbed text reply.
        if idx < len(self._raise_with):
            raise_exc = self._raise_with[idx]
        else:
            raise_exc = self._raise_with[-1] if self._raise_with else None
        if raise_exc is not None:
            raise raise_exc

        if idx < len(self._text_replies):
            text = self._text_replies[idx]
        else:
            text = self._text_replies[-1] if self._text_replies else None
        text = text if text is not None else f"stubbed AI reply #{idx}"

        if idx < len(self._tool_calls):
            tool_calls = self._tool_calls[idx]
        else:
            tool_calls = self._tool_calls[-1] if self._tool_calls else None

        return ChatResponse(
            content=text,
            model=request.model,
            prompt_tokens=10,
            completion_tokens=5,
            finish_reason="tool_use" if tool_calls else "stop",
            tool_calls=tool_calls,
        )


@pytest.fixture
def stub_llm_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> _CapturingLLM:
    """Install the stub LLM factory and yield the capturing object.

    The factory closes over the capturing LLM so the same object
    observes every ``LLMClient.chat`` invocation triggered by
    ``SimpleResponder``. Tests use ``set_text`` /
    ``set_tool_call`` / ``set_raises`` to drive behaviour.
    """
    llm = _CapturingLLM()
    monkeypatch.setattr(
        simple_responder_module,
        "_default_llm_client_factory",
        lambda _tenant_id: llm,
    )
    return llm


# ============================================================================
# No-threshold RAG service swap
# ============================================================================


class _NoThresholdRAGService(RAGService):
    """RAGService variant that disables the cosine-similarity threshold.

    Same trick as ``test_rag_e2e.py``. Mock embeddings have pairwise
    cosine similarities typically below the production default of
    ``0.3``; this subclass forces ``score_threshold=None`` so every
    Qdrant hit flows through to the LLM request.

    Only used in tests. Production code never imports this.
    """

    async def build_context_for_query(self, **kwargs: Any) -> RagContext:
        if "score_threshold" not in kwargs:
            kwargs["score_threshold"] = None
        return await super().build_context_for_query(**kwargs)


@pytest.fixture
def no_threshold_rag_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Swap ``simple_responder.RAGService`` for the no-threshold subclass.

    Applied as a side-effect on the imported symbol so
    ``SimpleResponder()`` (no kwargs) inherits the new class.
    """
    monkeypatch.setattr(
        simple_responder_module, "RAGService", _NoThresholdRAGService
    )


# ============================================================================
# Shared "topic-coded" article texts (with distinctive markers)
# ============================================================================


_SHIPPING_TEXT = (
    "MAGIC_PHRASE_SHIPPING_AGENT_001. Our standard shipping policy takes "
    "3 to 5 business days for domestic orders. International orders "
    "typically take 7 to 14 business days depending on the destination "
    "country and customs processing. Free shipping is available on all "
    "orders over 50 dollars. Tracking numbers are emailed within 24 hours "
    "of dispatch and can be tracked on the carrier's website. Expedited "
    "shipping options are available at checkout for an additional fee and "
    "reduce delivery time to 1 to 2 business days."
)

_COOKING_TEXT = (
    "MAGIC_PHRASE_COOKING_AGENT_002. Preheat the oven to 180 degrees "
    "celsius. Place the chicken breast in a roasting pan with olive oil, "
    "lemon, and fresh rosemary. Roast for 45 minutes until the juices run "
    "clear and the skin is golden. Let the meat rest for 10 minutes before "
    "slicing and serve with a side of roasted seasonal vegetables."
)