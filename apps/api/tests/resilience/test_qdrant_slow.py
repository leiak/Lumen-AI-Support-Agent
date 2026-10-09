"""Resilience test: Qdrant is slow / timing out during retrieval.

Contract pinned by this test (see :func:`knowledge.qdrant_client.search_chunks`
and :func:`knowledge.rag_service.RAGService.build_context_for_query`):

1. When the Qdrant ``query_points`` call raises ``asyncio.TimeoutError``,
   ``search_chunks`` returns ``[]`` instead of propagating the exception.
2. When ``retrieve_chunks`` therefore yields no hits (because Qdrant
   timed out), the RAG service returns an empty :class:`RagContext`
   (``chunk_count == 0``, ``system_message == ""``).
3. The empty context bubbles up to the LLM node as ``rag_messages=[]``,
   which means the customer's request still completes — the AI just
   answers without retrieval-augmented context.

The "RAG failures are non-fatal" invariant. A regression that propagated
the Qdrant timeout would 500 the conversation endpoint whenever the
vector store hiccupped.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from knowledge.qdrant_client import search_chunks


class _SlowQdrantClient:
    """Stand-in for ``AsyncQdrantClient`` whose ``query_points`` hangs / times out.

    ``search_chunks`` catches ``UnexpectedResponse`` first and any other
    ``Exception`` second; ``asyncio.TimeoutError`` lands in the second
    branch and the helper returns ``[]``. That is the production fail-open
    contract we want to pin here.
    """

    def __init__(self, *, exc: BaseException) -> None:
        self._exc = exc
        self.query_points = AsyncMock(side_effect=exc)
        # ``search_chunks`` also calls ``client.get_collection`` indirectly
        # when verifying the collection exists. With the helper itself
        # patched to use the mock we only need ``query_points``; the rest
        # of the interface is unused by :func:`search_chunks`.


@pytest.mark.asyncio
async def test_search_chunks_returns_empty_on_qdrant_timeout() -> None:
    """``search_chunks`` must swallow Qdrant timeouts and return ``[]``.

    This is the lowest-level fail-open boundary. The RAG service
    treats an empty hit list as "no relevant articles found", so a
    timeout here MUST be indistinguishable (to the caller) from a
    genuine miss.
    """
    with patch(
        "knowledge.qdrant_client.get_qdrant_client",
        return_value=_SlowQdrantClient(exc=TimeoutError()),
    ):
        result = await search_chunks(
            collection="article_chunks",
            query_vector=[0.0] * 4,
            tenant_id="t1",
            knowledge_base_id="kb1",
            top_k=5,
        )

    assert result == [], (
        "search_chunks MUST return [] on Qdrant timeout — never raise"
    )


@pytest.mark.asyncio
async def test_search_chunks_returns_empty_on_unexpected_response() -> None:
    """``search_chunks`` must swallow ``UnexpectedResponse`` too.

    The helper distinguishes HTTP-shape errors (logged with status_code)
    from generic transport errors. Both branches must produce ``[]``.
    This pins the second branch so a future refactor can't drop the
    ``except Exception`` safety net.
    """
    from qdrant_client.http.exceptions import UnexpectedResponse

    fake_exc = UnexpectedResponse(
        status_code=500,
        reason_phrase="internal server error",
        content=b"",
        headers={},
    )
    with patch(
        "knowledge.qdrant_client.get_qdrant_client",
        return_value=_SlowQdrantClient(exc=fake_exc),
    ):
        result = await search_chunks(
            collection="article_chunks",
            query_vector=[0.0] * 4,
            tenant_id="t1",
            knowledge_base_id="kb1",
            top_k=5,
        )

    assert result == []


@pytest.mark.asyncio
async def test_rag_service_returns_empty_context_when_qdrant_times_out() -> None:
    """End-to-end: a Qdrant timeout during retrieval must produce an
    empty ``RagContext`` (no exception, no partial system_message).

    Wires the full ``RAGService.build_context_for_query`` chain with
    the three downstream seams mocked:

    * KB lookup (``KnowledgeBaseRepository.get_default_for_tenant``) → fake KB
    * ``_assert_kb_exists`` (DB existence check) → fake KB (skip the DB)
    * ``embed_texts`` → deterministic fake vector (skip OpenAI)
    * ``query_points`` → ``asyncio.TimeoutError`` (the actual fault)

    With Qdrant timing out, the retriever sees ``scored_points == []``
    and returns ``[]``. The RAG service treats that as "no hits" and
    builds an empty :class:`RagContext`. The LLM node later sees
    ``rag_messages=[]`` and answers without RAG context — exactly what
    we want during a Qdrant outage.
    """
    from knowledge.rag_service import RAGService
    from llm_client.types import EmbeddingResult

    fake_kb = MagicMock()
    fake_kb.id = "kb1"
    fake_kb.name = "Test KB"
    fake_kb.embedding_model = "text-embedding-3-small"

    fake_embedding = EmbeddingResult(
        vectors=[[0.1] * 4],
        model="text-embedding-3-small",
        prompt_tokens=0,
        total_tokens=0,
    )

    with patch(
        "knowledge.qdrant_client.get_qdrant_client",
        return_value=_SlowQdrantClient(exc=TimeoutError()),
    ), patch(
        "knowledge.retriever.embed_texts",
        AsyncMock(return_value=fake_embedding),
    ), patch(
        "knowledge.retriever._assert_kb_exists",
        AsyncMock(return_value=fake_kb),
    ), patch(
        "knowledge.rag_service.KnowledgeBaseRepository",
    ) as mock_repo_cls:
        mock_repo_cls.return_value.get_default_for_tenant = AsyncMock(
            return_value=fake_kb
        )

        rag = RAGService()
        ctx = await rag.build_context_for_query(
            tenant_id="t1",
            query="what is the weather forecast",
            top_k=5,
        )

    assert ctx.chunk_count == 0, (
        "Qdrant timeout must propagate as zero chunks to the caller"
    )
    assert ctx.system_message == "", (
        "Empty chunks must produce an empty system_message — never a partial block"
    )
    # The KB WAS resolved (so its id surfaces for observability) but no
    # chunks came back. The "tenant has no KB" path uses '' for both
    # fields — see :func:`RAGService._empty_context`. The Qdrant-timeout
    # path lands here in the no-hits branch, which carries the resolved
    # KB id so a debug operator can see which KB would have been used.
    assert ctx.knowledge_base_id == "kb1", (
        "No-hits context must carry the resolved KB id for observability"
    )
    assert ctx.knowledge_base_name == "Test KB"
    assert ctx.retrieval_score_max == 0.0


__all__ = [
    "test_rag_service_returns_empty_context_when_qdrant_times_out",
    "test_search_chunks_returns_empty_on_qdrant_timeout",
    "test_search_chunks_returns_empty_on_unexpected_response",
]
