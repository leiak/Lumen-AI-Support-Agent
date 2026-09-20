"""Unit tests for KBDraftGenerator (Stage 18 / Task 7).

Tests happy-path structured output, graceful fallback on LLM failure,
max_questions truncation, and Pydantic length validation on KBDraft.

All LLM calls are mocked (AsyncMock). The contract:
- KBDraftGenerator.generate(questions, llm_client=...) returns a KBDraft
- On LLM failure, returns a fallback KBDraft (placeholder title + raw qs)
- Prompts sent to the LLM are truncated to max_questions
- KBDraft Pydantic model enforces title <= 200, body <= 2000 chars
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from history_mining.draft_generator import KBDraft, KBDraftGenerator


@pytest.fixture
def generator() -> KBDraftGenerator:
    return KBDraftGenerator(llm_client_factory=lambda: AsyncMock())


@pytest.mark.asyncio
async def test_generate_draft_from_representative_questions(generator: KBDraftGenerator) -> None:
    """5 questions -> structured KBDraft via LLM."""
    mock_client = MagicMock()
    mock_draft = KBDraft(
        title="How to reset password",
        body="Go to settings > account > reset password",
        suggested_tags=["account", "password"],
    )
    mock_client.chat_with_structured_output = AsyncMock(return_value=mock_draft)

    questions = [
        "How do I reset my password?",
        "Forgot password, what to do?",
        "Can't log in, need password reset",
        "Reset password link not working",
        "Password reset email not received",
    ]

    draft = await generator.generate(questions, llm_client=mock_client)

    assert draft.title == "How to reset password"
    assert "settings" in draft.body
    mock_client.chat_with_structured_output.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_falls_back_on_llm_failure(generator: KBDraftGenerator) -> None:
    """LLM failure -> return KBDraft with placeholder + warning."""
    mock_client = MagicMock()
    mock_client.chat_with_structured_output = AsyncMock(
        side_effect=RuntimeError("LLM down")
    )

    draft = await generator.generate(["q1", "q2", "q3"], llm_client=mock_client)

    assert draft.title  # placeholder
    assert draft.body  # placeholder


@pytest.mark.asyncio
async def test_generate_truncates_to_max_questions(generator: KBDraftGenerator) -> None:
    """10 questions -> only first 5 are sent to LLM."""
    mock_client = MagicMock()
    mock_client.chat_with_structured_output = AsyncMock(
        return_value=KBDraft(title="t", body="b", suggested_tags=[])
    )

    questions = [f"q{i}" for i in range(10)]
    await generator.generate(questions, llm_client=mock_client, max_questions=3)

    # Inspect the prompt sent — should contain only 3 questions
    call_kwargs = mock_client.chat_with_structured_output.call_args.kwargs
    messages = call_kwargs["messages"]
    prompt_content = messages[1]["content"]
    assert prompt_content.count("- q") == 3


def test_kbdraft_model_validates_lengths() -> None:
    """Pydantic model rejects over-length title/body."""
    with pytest.raises(Exception):  # PydanticValidationError or ValueError
        KBDraft(title="x" * 201, body="b", suggested_tags=[])
    with pytest.raises(Exception):
        KBDraft(title="t", body="x" * 2001, suggested_tags=[])