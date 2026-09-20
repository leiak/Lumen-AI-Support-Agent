"""Tests for ``search_multimodal_kb`` LangChain tool factory (Stage 17 Task 6).

The tool is the multimodal SIBLING of ``search_internal_kb`` (Stage 12
Task 1). Closure-injects ``rag_service`` + ``kb_repository`` +
``qdrant_client`` at compile time, and reads per-turn
``tenant_id`` / ``conversation_id`` from the module-level
:data:`agent.graph.tools._escalation_ctx` ContextVar.

The retriever is exercised via the real
:class:`knowledge.multimodal.retriever.MultimodalRetriever` class —
we mock at the Qdrant client boundary so the test runs without a
real Qdrant server. ``embed_texts`` is also mocked so the test
doesn't depend on the configured embedding provider.

These tests pin:

1. **Tool name** — the LLM sees it advertised as
   ``search_multimodal_kb`` (NOT ``search_internal_kb``).
2. **Image-query placeholder** — when ``doubao_vision_api_key`` is
   set, the tool passes a 1024-dim zero vector to the retriever
   so the ``kb_image_vectors`` collection IS queried. Without
   vision configured, it passes ``None`` so the retriever skips
   the image search (the existing graceful-degradation contract).
3. **Tenant isolation** — the kb_slug lookup and the retriever
   call both receive the ``tenant_id`` from the ContextVar (never
   from LLM-supplied args).
"""
from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.graph.tools import (
    bind_escalation_context,
    make_search_multimodal_kb_tool,
    reset_escalation_context,
)


@pytest.fixture(autouse=True)
def _mock_doubao_vision_config() -> Iterator[None]:
    """Patch ``get_settings`` so the tool sees ``doubao_vision_api_key`` set.

    Without this patch the dev env's empty key would cause the
    tool to skip the image-query placeholder entirely. We want
    to exercise the path that actually queries the image
    collection, so we set a non-empty key here.
    """
    settings = MagicMock()
    settings.doubao_vision_api_key = "fake-vision-key"
    settings.doubao_vision_base_url = "https://fake"
    settings.doubao_vision_model = "doubao-embedding-vision"
    with patch("core.config.get_settings", return_value=settings):
        yield


@pytest.fixture
def mock_rag_service():
    svc = MagicMock()
    svc.retrieve = AsyncMock(return_value=[])
    return svc


@pytest.fixture
def mock_kb_repo():
    repo = MagicMock()
    kb = MagicMock()
    kb.id = "kb-stub-multimodal"
    repo.find_by_slug = AsyncMock(return_value=kb)
    return repo


@pytest.fixture
def mock_qdrant_client():
    qdrant = MagicMock()
    # ``query_points`` returns a ``QueryResponse`` with a
    # ``points`` attribute (NameList[ScoredPoint]). Empty list =
    # no hits, which is the cleanest assertion-friendly shape.
    response = MagicMock()
    response.points = []
    qdrant.query_points = AsyncMock(return_value=response)
    return qdrant


@pytest.fixture
def mock_embed_texts():
    """Patch ``embed_texts`` so the tool's text-query path doesn't
    hit the real embedding provider."""
    with patch(
        "llm_client.embeddings.embed_texts",
        AsyncMock(
            return_value=MagicMock(vectors=[[0.1] * 1024])
        ),
    ) as mock:
        yield mock


def test_tool_name_is_search_multimodal_kb(
    mock_rag_service, mock_kb_repo, mock_qdrant_client
):
    """The LangChain tool advertises the ``search_multimodal_kb`` name."""
    tool = make_search_multimodal_kb_tool(
        rag_service=mock_rag_service,
        kb_repository=mock_kb_repo,
        qdrant_client=mock_qdrant_client,
    )
    assert tool.name == "search_multimodal_kb"


@pytest.mark.asyncio
async def test_tool_passes_zero_vector_image_query_when_vision_configured(
    mock_rag_service,
    mock_kb_repo,
    mock_qdrant_client,
    mock_embed_texts,
):
    """When ``doubao_vision_api_key`` is set, the tool must pass a
    1024-dim zero vector as ``image_query_embedding`` so the
    ``kb_image_vectors`` collection is actually queried.

    Reviewer finding (Task 6 code review): without this, the tool
    was equivalent to text-only RAG — the "multimodal" contract
    was misleading.
    """
    tool = make_search_multimodal_kb_tool(
        rag_service=mock_rag_service,
        kb_repository=mock_kb_repo,
        qdrant_client=mock_qdrant_client,
    )
    token = bind_escalation_context(tenant_id="t1", conversation_id="conv1")
    try:
        await tool.ainvoke(
            {"query": "diagram", "kb_slug": "support", "top_k": 3}
        )
    finally:
        reset_escalation_context(token)

    # qdrant.query_points was called at least twice (text +
    # image search). Both calls receive the appropriate MUST-filter
    # and collection name.
    assert mock_qdrant_client.query_points.await_count == 2

    # Pull the two calls and identify which is the image search
    # by ``collection_name``.
    call_kwargs = [
        c.kwargs for c in mock_qdrant_client.query_points.await_args_list
    ]
    by_collection: dict[str, dict] = {}
    for kw in call_kwargs:
        by_collection[kw["collection_name"]] = kw

    # The text search uses the M1 ``article_chunks`` collection.
    text_call = by_collection.get("article_chunks")
    assert text_call is not None
    assert text_call["query"] == [0.1] * 1024
    # The image search uses ``kb_image_vectors`` and receives
    # a 1024-dim zero vector (the placeholder).
    image_call = by_collection.get("kb_image_vectors")
    assert image_call is not None
    assert image_call["query"] == [0.0] * 1024
    assert len(image_call["query"]) == 1024

    # The image call's filter MUST scope to the tenant + the
    # kb_slug the LLM supplied — no leak across tenants.
    image_filter = image_call["query_filter"]
    must_conditions = image_filter.must
    keys = {c.key for c in must_conditions}
    assert "tenant_id" in keys
    assert "kb_slug" in keys


@pytest.mark.asyncio
async def test_tool_passes_none_image_query_when_vision_unconfigured(
    mock_rag_service,
    mock_kb_repo,
    mock_qdrant_client,
    mock_embed_texts,
):
    """When ``doubao_vision_api_key`` is empty, the tool must pass
    ``None`` as ``image_query_embedding`` so the retriever skips
    the image search entirely (graceful-degradation contract).

    This pins the second branch of the ``if settings.doubao_vision_api_key``
    check in the tool body.
    """
    settings = MagicMock()
    settings.doubao_vision_api_key = ""  # not configured
    with patch("core.config.get_settings", return_value=settings):
        tool = make_search_multimodal_kb_tool(
            rag_service=mock_rag_service,
            kb_repository=mock_kb_repo,
            qdrant_client=mock_qdrant_client,
        )
        token = bind_escalation_context(
            tenant_id="t1", conversation_id="conv1"
        )
        try:
            await tool.ainvoke(
                {"query": "diagram", "kb_slug": "support", "top_k": 3}
            )
        finally:
            reset_escalation_context(token)

    # Only the text search runs — the image search is skipped
    # because image_query_embedding is None (per the retriever's
    # ``if image_query_embedding is not None and kb_slug`` guard).
    assert mock_qdrant_client.query_points.await_count == 1
    text_call = mock_qdrant_client.query_points.await_args.kwargs
    assert text_call["collection_name"] == "article_chunks"


@pytest.mark.asyncio
async def test_tool_requires_tenant_context(
    mock_rag_service, mock_kb_repo, mock_qdrant_client
):
    """No escalation context bound → tool returns an error string
    rather than raising (so the LLM-node tool loop can record
    the error and continue)."""
    tool = make_search_multimodal_kb_tool(
        rag_service=mock_rag_service,
        kb_repository=mock_kb_repo,
        qdrant_client=mock_qdrant_client,
    )
    result = await tool.ainvoke({"query": "x"})
    assert "error" in result.lower() or "context" in result.lower()


@pytest.mark.asyncio
async def test_tool_resolves_kb_slug_from_context(
    mock_rag_service,
    mock_kb_repo,
    mock_qdrant_client,
    mock_embed_texts,
):
    """The kb_slug lookup uses the tenant_id from the ContextVar,
    NEVER from LLM-supplied args. Pins the cross-tenant isolation
    contract."""
    tool = make_search_multimodal_kb_tool(
        rag_service=mock_rag_service,
        kb_repository=mock_kb_repo,
        qdrant_client=mock_qdrant_client,
    )
    token = bind_escalation_context(tenant_id="t-iso", conversation_id="c1")
    try:
        await tool.ainvoke(
            {"query": "x", "kb_slug": "billing", "top_k": 5}
        )
    finally:
        reset_escalation_context(token)

    mock_kb_repo.find_by_slug.assert_awaited_once_with(
        tenant_id="t-iso", slug="billing"
    )
    # The text-search call's filter MUST carry tenant_id=t-iso.
    text_call = mock_qdrant_client.query_points.await_args_list[0].kwargs
    keys = {c.key for c in text_call["query_filter"].must}
    values_by_key = {
        c.key: c.match.value for c in text_call["query_filter"].must
    }
    assert keys == {"tenant_id", "knowledge_base_id"}
    assert values_by_key["tenant_id"] == "t-iso"
    assert values_by_key["knowledge_base_id"] == "kb-stub-multimodal"
