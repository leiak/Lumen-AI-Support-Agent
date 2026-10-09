"""Tests for ``agent.graph._invocation.assemble_llm_messages``.

The original :func:`agent.graph.nodes.make_llm_node` built the
``ChatMessage`` list inline (system prompt + RAG context + user
history) and mixed that with the 500+ lines of LLM orchestration.
These tests pin the *order* of that list so the extracted helper
stays a faithful copy.

The helper is a pure function — no LLM client, no graph state,
no async — so we only need to exercise three cases:

1. **Prepend system prompt** — a fresh request always starts
   with the system prompt as the leading ``system`` row.
2. **RAG messages land second as ``system``** — the retrieve
   node's synthetic ``SystemMessage`` rows flow in directly
   after the system prompt, *without* losing their
   ``SystemMessage`` identity (they become another ``system``
   row, not a ``user`` row).
3. **User history follows** — every LangChain
   ``BaseMessage`` in the history is converted via the existing
   ``_to_llm_chat_message`` helper and appended in order.

The role enum is ``MessageRole`` (``llm_client.types``). We
compare against the enum directly — that's the contract the rest
of the codebase uses (see ``_to_llm_role`` in ``nodes.py``).
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from llm_client.types import MessageRole


def test_assemble_llm_messages_prepends_system_and_rag() -> None:
    """``assemble_llm_messages`` returns a list of LLM chat messages
    with system_prompt first, then rag_messages (as system), then
    user messages.
    """
    from agent.graph._invocation import assemble_llm_messages

    # Create the input messages
    messages = [HumanMessage(content="hi")]
    rag = [SystemMessage(content="rag context")]

    # Call the helper
    out = assemble_llm_messages(
        system_prompt="sys",
        rag_messages=rag,
        messages=messages,
    )

    # Verify the output structure — system + rag + user = 3 rows
    assert len(out) == 3

    # First message: system prompt
    assert out[0].role == MessageRole.SYSTEM
    assert out[0].content == "sys"

    # Second message: rag context (still SYSTEM)
    assert out[1].role == MessageRole.SYSTEM
    assert out[1].content == "rag context"

    # Third message: user query
    assert out[2].role == MessageRole.USER
    assert out[2].content == "hi"


def test_assemble_llm_messages_empty_rag_keeps_order() -> None:
    """No RAG context → system prompt still leads, user history
    follows. Guards against regressions where an empty
    ``rag_messages`` list would shift the user rows into the
    leading position.
    """
    from agent.graph._invocation import assemble_llm_messages

    out = assemble_llm_messages(
        system_prompt="sys",
        rag_messages=[],
        messages=[HumanMessage(content="user1"), HumanMessage(content="user2")],
    )

    assert len(out) == 3
    assert out[0].role == MessageRole.SYSTEM
    assert out[0].content == "sys"
    assert out[1].role == MessageRole.USER
    assert out[1].content == "user1"
    assert out[2].role == MessageRole.USER
    assert out[2].content == "user2"


def test_assemble_llm_messages_preserves_role_mapping() -> None:
    """``AIMessage`` in the history maps to ``ASSISTANT``, not
    ``USER``. This guards ``_to_llm_chat_message`` — the helper
    the assembler delegates to — against accidental role
    flattening.
    """
    from agent.graph._invocation import assemble_llm_messages

    out = assemble_llm_messages(
        system_prompt="sys",
        rag_messages=[],
        messages=[
            HumanMessage(content="hi"),
            AIMessage(content="hello"),
            HumanMessage(content="bye"),
        ],
    )

    assert len(out) == 4
    assert out[0].role == MessageRole.SYSTEM
    assert out[1].role == MessageRole.USER
    assert out[2].role == MessageRole.ASSISTANT
    assert out[3].role == MessageRole.USER


@pytest.mark.parametrize(
    "rag_count",
    [1, 2, 3],
    ids=["one_rag", "two_rag", "three_rag"],
)
def test_assemble_llm_messages_multiple_rag_rows_in_order(
    rag_count: int,
) -> None:
    """All RAG rows land directly after the system prompt, in the
    original order. The customer history follows the RAG block.
    Parametrized over 1-3 RAG rows so a regression that drops a
    row would show up in a single failing case rather than a
    mixed failure.
    """
    from agent.graph._invocation import assemble_llm_messages

    rag_rows = [SystemMessage(content=f"rag{i}") for i in range(rag_count)]
    history = [HumanMessage(content="hi")]

    out = assemble_llm_messages(
        system_prompt="sys",
        rag_messages=rag_rows,
        messages=history,
    )

    # Expected length: 1 system prompt + N rag rows + 1 user row.
    assert len(out) == 2 + rag_count

    # System prompt leads.
    assert out[0].role == MessageRole.SYSTEM
    assert out[0].content == "sys"

    # All rag rows are SYSTEM and in original order.
    for i in range(rag_count):
        assert out[1 + i].role == MessageRole.SYSTEM
        assert out[1 + i].content == f"rag{i}"

    # User row is last.
    assert out[-1].role == MessageRole.USER
    assert out[-1].content == "hi"
