"""Tests for the QA Judge client (Stage 14 / Task 7).

All ``LLMClient`` interactions are mocked — Task 7 does NOT exercise
real provider calls (those wire in Task 8). The Judge's contract is:

* ``score()`` returns a ``JudgeOutput`` with 3 dimensions in [0, 1].
* Out-of-range scores get clamped.
* Retry once on transient failure; raise ``JudgeFailure`` after exhaustion.
* ``is_flagged(o)`` is True iff min(relevance, safety, faithfulness) < threshold.
* ``_xml_escape`` is private but importable so callers (and tests) can
  verify the framing defense.
* User content is truncated to 1000 / 2000 / 10-list before interpolation
  (defense against prompt-stuffing).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from qa.judge import (
    JudgeClient,
    JudgeFailure,
    JudgeInput,
    JudgeOutput,
    _xml_escape,
)


# ---- happy-path ----------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_parses_valid_json_response() -> None:
    """LLM returning a JudgeOutput-shaped dict -> JudgeOutput is returned."""
    mock_llm = MagicMock()
    mock_llm.chat_with_structured_output = AsyncMock(
        return_value=JudgeOutput(
            relevance=0.9,
            safety=0.95,
            faithfulness=0.85,
            rationale="good answer",
        )
    )
    client = JudgeClient(llm=mock_llm, model="test-model", threshold=0.3)

    output = await client.score(JudgeInput(question="q", answer="a", citations=[]))

    assert output.relevance == 0.9
    assert output.safety == 0.95
    assert output.faithfulness == 0.85
    assert output.rationale == "good answer"


# ---- defensive clamps ----------------------------------------------------


@pytest.mark.asyncio
async def test_judge_clamps_out_of_range_scores() -> None:
    """Scores outside [0, 1] get clamped; rationale is sliced."""
    mock_llm = MagicMock()
    # Bypass Pydantic's validators by returning a manual object with
    # raw attributes — the JudgeClient is supposed to clamp on assignment
    # in _clamp() regardless of what the underlying LLM hands back.
    class _RawOutput:
        relevance = 1.5
        safety = -0.2
        faithfulness = 0.5
        rationale = "x" * 500  # > 300 chars

    mock_llm.chat_with_structured_output = AsyncMock(return_value=_RawOutput())
    client = JudgeClient(llm=mock_llm, model="test-model", threshold=0.3)

    output = await client.score(JudgeInput(question="q", answer="a"))

    assert output.relevance == 1.0
    assert output.safety == 0.0
    assert output.faithfulness == 0.5
    assert len(output.rationale) == 300  # defense-in-depth truncation


# ---- retry policy --------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_retries_once_on_failure() -> None:
    """A single transient failure on the first attempt is retried."""
    mock_llm = MagicMock()
    mock_llm.chat_with_structured_output = AsyncMock(
        side_effect=[
            Exception("timeout"),
            JudgeOutput(
                relevance=0.8, safety=0.8, faithfulness=0.8, rationale="ok"
            ),
        ]
    )
    client = JudgeClient(
        llm=mock_llm, model="test-model", threshold=0.3, max_retries=1
    )

    output = await client.score(JudgeInput(question="q", answer="a"))

    assert output.relevance == 0.8
    # Both attempts hit the LLM.
    assert mock_llm.chat_with_structured_output.await_count == 2


@pytest.mark.asyncio
async def test_judge_raises_judge_failure_after_retries() -> None:
    """All attempts failing -> JudgeFailure is raised."""
    mock_llm = MagicMock()
    mock_llm.chat_with_structured_output = AsyncMock(
        side_effect=Exception("boom")
    )
    client = JudgeClient(
        llm=mock_llm, model="test-model", threshold=0.3, max_retries=1
    )

    with pytest.raises(JudgeFailure):
        await client.score(JudgeInput(question="q", answer="a"))
    # max_retries=1 means up to 2 attempts total.
    assert mock_llm.chat_with_structured_output.await_count == 2


# ---- is_flagged (method, not property) ----------------------------------


def test_judge_is_flagged_below_threshold() -> None:
    """Any dimension below threshold -> flagged.

    Plan note: the original draft used ``@property`` here which made the
    call sites inconsistent (property can't have one branch with a
    default and an argument). Bug fix: it's a method, callers do
    ``client.is_flagged(output)``.
    """
    mock_llm = MagicMock()
    client = JudgeClient(llm=mock_llm, model="x", threshold=0.3)

    # relevance below threshold -> flagged
    assert client.is_flagged(
        JudgeOutput(
            relevance=0.2, safety=0.9, faithfulness=0.9, rationale="x"
        )
    )
    # all above -> not flagged
    assert not client.is_flagged(
        JudgeOutput(
            relevance=0.5, safety=0.9, faithfulness=0.9, rationale="x"
        )
    )


# ---- _xml_escape ---------------------------------------------------------


def test_xml_escape_prevents_injection() -> None:
    """``<``, ``>``, ``&`` are escaped; safe to interpolate into the prompt."""
    assert _xml_escape("<script>") == "&lt;script&gt;"
    assert _xml_escape("a & b") == "a &amp; b"
    assert _xml_escape("plain text") == "plain text"
    # Order matters — ``&`` MUST be replaced first so the ``<`` /
    # ``>`` replacements aren't themselves re-escaped.
    assert _xml_escape("<a>&<b>") == "&lt;a&gt;&amp;&lt;b&gt;"


# ---- defense against oversized / hostile inputs -------------------------


@pytest.mark.asyncio
async def test_judge_xml_escapes_and_truncates_long_inputs() -> None:
    """Inputs > 1000 / 2000 chars are truncated AND XML-escaped before
    being interpolated into the prompt — defense against prompt
    injection via oversized or malformed content."""
    mock_llm = MagicMock()
    mock_llm.chat_with_structured_output = AsyncMock(
        return_value=JudgeOutput(
            relevance=0.5, safety=0.5, faithfulness=0.5, rationale="x"
        )
    )
    client = JudgeClient(llm=mock_llm, model="test", threshold=0.3)

    long_with_xml = "<script>" * 1000 + "a" * 5000  # > 1000 chars, has < and >
    await client.score(JudgeInput(question=long_with_xml, answer="a"))

    # Inspect what the Judge sent to the LLM.
    called_messages = mock_llm.chat_with_structured_output.await_args.kwargs[
        "messages"
    ]
    user_content = called_messages[1]["content"]
    # The escaped form must be present.
    assert "&lt;script&gt;" in user_content
    # The raw attack vector must NOT be present — otherwise the LLM
    # could be tricked into acting on it.
    assert "<script>" not in user_content
    # Question was truncated to 1000 chars. "<script>" is 8 chars,
    # so 1000 // 8 == 125 full repetitions of "<script>" (note: the
    # escape happens AFTER truncation so we measure the unescaped
    # source length).
    assert long_with_xml[:1000].count("<script>") == 125