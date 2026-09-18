"""Tests for ``search_internal_kb`` LangChain tool factory (Stage 12 Task 1).

The tool is a closure that injects ``rag_service`` + ``kb_repository`` at
compile time and reads per-turn ``tenant_id`` / ``conversation_id`` from
the module-level :data:`agent.graph.tools._escalation_ctx` ContextVar.

These tests use ``MagicMock`` for both dependencies so the tool's
*contract* is verified without spinning up Postgres / Qdrant. The
``rag_service.retrieve`` and ``kb_repository.find_by_slug`` interfaces
here match the production contract that Stage 12 Task 2 will wire up.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from agent.graph.tools import (
    bind_escalation_context,
    make_search_internal_kb_tool,
    reset_escalation_context,
)


@pytest.fixture
def mock_rag_service():
    svc = MagicMock()
    svc.retrieve = AsyncMock(return_value=[
        {"chunk_id": "c1", "article_title": "Password Reset", "score": 0.85, "content": "To reset..."},
        {"chunk_id": "c2", "article_title": "Account Management", "score": 0.72, "content": "Navigate..."},
    ])
    return svc


@pytest.fixture
def mock_kb_repo():
    repo = MagicMock()
    # Stage 12 will define this method on KnowledgeRepository; if it doesn't
    # exist yet, make_search_internal_kb_tool MUST tolerate the absence and
    # pass knowledge_base_id=None (so the LLM can fall back to "all KBs").
    repo.find_by_slug = AsyncMock(return_value=MagicMock(id="kb-acme"))
    return repo


@pytest.mark.asyncio
async def test_search_internal_kb_returns_top_k_chunks(mock_rag_service, mock_kb_repo):
    tool = make_search_internal_kb_tool(rag_service=mock_rag_service, kb_repository=mock_kb_repo)
    token = bind_escalation_context(tenant_id="t1", conversation_id="conv1")
    try:
        result = await tool.ainvoke({"query": "reset password", "top_k": 5})
    finally:
        reset_escalation_context(token)
    assert "Password Reset" in result
    assert "c1" in result
    mock_rag_service.retrieve.assert_awaited_once()
    _, kwargs = mock_rag_service.retrieve.call_args
    assert kwargs["tenant_id"] == "t1"
    assert kwargs["conversation_id"] == "conv1"
    assert kwargs["top_k"] == 5


@pytest.mark.asyncio
async def test_search_internal_kb_clamps_top_k_to_20(mock_rag_service, mock_kb_repo):
    """Stage 12 Task 1 polish: top_k has Pydantic ``le=20`` + body clamp.

    The Pydantic constraint (``le=20``) rejects out-of-range values at
    the schema layer before the tool body even runs, so the LLM cannot
    smuggle a bad value past LangChain's args validation. The body
    ``max(1, min(top_k, 20))`` is a defensive belt-and-suspenders net
    for any path that bypasses the schema (e.g. direct ainvoke calls
    in tests).
    """
    from pydantic import ValidationError

    tool = make_search_internal_kb_tool(rag_service=mock_rag_service, kb_repository=mock_kb_repo)
    token = bind_escalation_context(tenant_id="t1", conversation_id="conv1")
    try:
        with pytest.raises(ValidationError):
            await tool.ainvoke({"query": "x", "top_k": 100})
    finally:
        reset_escalation_context(token)


@pytest.mark.asyncio
async def test_search_internal_kb_filters_by_kb_slug(mock_rag_service, mock_kb_repo):
    tool = make_search_internal_kb_tool(rag_service=mock_rag_service, kb_repository=mock_kb_repo)
    token = bind_escalation_context(tenant_id="t1", conversation_id="conv1")
    try:
        await tool.ainvoke({"query": "x", "kb_slug": "acme-billing", "top_k": 3})
    finally:
        reset_escalation_context(token)
    mock_kb_repo.find_by_slug.assert_awaited_once_with("t1", "acme-billing")
    _, kwargs = mock_rag_service.retrieve.call_args
    assert kwargs["knowledge_base_id"] == "kb-acme"
    assert kwargs["top_k"] == 3


@pytest.mark.asyncio
async def test_search_internal_kb_requires_tenant_context(mock_rag_service, mock_kb_repo):
    tool = make_search_internal_kb_tool(rag_service=mock_rag_service, kb_repository=mock_kb_repo)
    # No context bound — ContextVar default is ("", "") per tools.py line 77.
    # The tool should detect this and return an error string (NOT raise) so
    # the llm_node tool-loop in Task 2 can record it and continue.
    result = await tool.ainvoke({"query": "x"})
    assert "error" in result.lower() or "context" in result.lower()
