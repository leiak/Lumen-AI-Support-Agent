"""Resilience test: LLM client times out during the chat call.

Contract pinned by this test (see :func:`agent.graph.nodes.make_llm_node`):

1. When the LLM client raises ``asyncio.TimeoutError`` (or any other
   exception type not in the typed catch set), ``make_llm_node``
   returns ``{"final_text": FALLBACK_MESSAGE, "escalated": False, ...}``.
2. The node MUST NEVER raise — a raise here would crash the LangGraph
   turn and leave the customer without a response.
3. The fallback is observable: ``final_text`` is the well-known
   :data:`FALLBACK_MESSAGE` string, ``escalated`` is ``False`` so the
   graph routes to the normal text path (not the escalation terminal).

Two fault shapes are exercised because the typed and untyped catch
branches both need coverage:

* ``asyncio.TimeoutError`` — falls through the typed set, lands in
  the ``except Exception`` safety net (via :func:`safe_respond`).
* :class:`ProviderUnavailable` — caught by the typed set directly.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import HumanMessage

from agent.graph.nodes import FALLBACK_MESSAGE, make_llm_node
from agent.graph.state import AgentState
from llm_client.client import LLMClient


def _state_with_human_message(content: str = "hi") -> AgentState:
    """Build an ``AgentState`` with one customer message.

    The node reads ``messages`` to find the latest customer turn; an
    empty messages list would short-circuit to the empty fallback
    path (``_latest_customer_text`` returns ``None``) and not
    exercise the LLM call at all.
    """
    return AgentState(
        tenant_id="t1",
        conversation_id="c1",
        messages=[HumanMessage(content=content)],
        rag_messages=[],
        final_text=None,
    )


def _factory_returning(client: LLMClient) -> Callable[[str], Awaitable[LLMClient]]:
    """Build a per-tenant LLM factory that always yields ``client``."""

    async def _factory(tenant_id: str) -> LLMClient:
        return client

    return _factory


@pytest.mark.asyncio
async def test_llm_timeout_returns_fallback_message() -> None:
    """``asyncio.TimeoutError`` from ``client.chat`` → fallback message.

    The bare ``except Exception`` safety net in :func:`make_llm_node`
    (delegated to :func:`agent.graph._safe.safe_respond`) catches this
    and returns the FALLBACK_MESSAGE. The node MUST NOT raise.
    """
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=TimeoutError())

    node = make_llm_node(
        llm_client_factory=_factory_returning(llm),
        model="claude-haiku-4-5",
        tools=[],
    )

    result = await node(_state_with_human_message())

    assert result["final_text"] == FALLBACK_MESSAGE, (
        "LLM timeout must surface as FALLBACK_MESSAGE, not an exception"
    )
    assert result["escalated"] is False, (
        "Timeout is NOT an escalation — the customer still gets the normal text path"
    )
    assert result["tool_iterations"] == 0
    # Sanity: the LLM was actually called (and failed). A regression
    # that short-circuited BEFORE the LLM would still pass the
    # assertion above but would miss the point of this test.
    assert llm.chat.await_count == 1


@pytest.mark.asyncio
async def test_llm_provider_unavailable_returns_fallback_message() -> None:
    """Variant: :class:`ProviderUnavailable` → same fallback path.

    The typed-catch branch in :func:`make_llm_node` handles
    ``ProviderUnavailable`` directly (the documented "transient
    infrastructure failure" mode). The fallback shape MUST match the
    timeout branch so callers can rely on a uniform response contract.
    """
    from llm_client.exceptions import ProviderUnavailable

    llm = MagicMock()
    llm.chat = AsyncMock(
        side_effect=ProviderUnavailable("anthropic 503 service unavailable")
    )

    node = make_llm_node(
        llm_client_factory=_factory_returning(llm),
        model="claude-haiku-4-5",
        tools=[],
    )

    result = await node(_state_with_human_message("please help"))

    assert result["final_text"] == FALLBACK_MESSAGE
    assert result["escalated"] is False
    assert result["tool_iterations"] == 0
    assert llm.chat.await_count == 1


@pytest.mark.asyncio
async def test_llm_arbitrary_exception_returns_fallback_message() -> None:
    """Defence-in-depth: an unexpected exception class also returns the fallback.

    A regression that removed the ``except Exception`` safety net
    around :func:`make_llm_node`'s LLM call would crash the LangGraph
    turn on any unanticipated error (a programming bug, a third-party
    library raising something exotic). The safety net exists to keep
    the customer-turn never-fail invariant.
    """
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=RuntimeError("unexpected library bug"))

    node = make_llm_node(
        llm_client_factory=_factory_returning(llm),
        model="claude-haiku-4-5",
        tools=[],
    )

    result: dict[str, Any] = await node(_state_with_human_message("hello"))

    assert result["final_text"] == FALLBACK_MESSAGE, (
        "The bare-except safety net MUST catch any exception type — "
        "removing it would regress the customer-turn never-fail invariant"
    )
    assert result["escalated"] is False


__all__ = [
    "test_llm_arbitrary_exception_returns_fallback_message",
    "test_llm_provider_unavailable_returns_fallback_message",
    "test_llm_timeout_returns_fallback_message",
]
