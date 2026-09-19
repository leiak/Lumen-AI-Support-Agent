"""Real-time QA judge client.

Stage 14 / Task 7 — a small LLM scores each AI reply on three
dimensions (relevance / safety / faithfulness, 0-1). The judge runs
in an Arq background task (added in Task 8) and never blocks the main
conversation path.

Prompt framing
--------------

We send two messages:

* **system**: a one-shot instruction telling the LLM what to score and
  what JSON shape to emit.
* **user**: the formatted template with the customer question, AI
  answer, and citations inside XML tags.

User content is XML-escaped (so an attacker can't break out of the
``<user_question>`` frame) and truncated to bounded lengths (so the
prompt size is capped at ~3000 chars even with hostile input).

Retry policy
------------

``max_retries`` is the number of *additional* attempts after the
first; ``max_retries=1`` (default) means up to 2 attempts total. We
catch ``Exception`` (deliberately broad) because the Judge LLM
provider surface is still evolving in Task 8 — the structured-output
contract is what we care about, not the failure mode. A
:class:`JudgeFailure` is raised after exhaustion so the worker can
record the failure against ``lumen_qa_judge_failures_total``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from core.config import get_settings
from core.logging import get_logger

if TYPE_CHECKING:
    from llm_client.client import LLMClient

log = get_logger(__name__)


class JudgeFailure(Exception):
    """Raised when a Judge LLM call fails after retries are exhausted."""


class JudgeInput(BaseModel):
    """Inputs to the judge. Caller fills these in from the
    conversation turn + RAG retrieval."""

    question: str
    answer: str
    citations: list[str] = Field(default_factory=list)


class JudgeOutput(BaseModel):
    """Output from the judge LLM. All scores are clamped to [0, 1].

    ``rationale`` is bounded to 300 characters — the Pydantic constraint
    is the source of truth; the DB layer (``MessageQaScore.rationale``)
    is intentionally ``Text`` (unbounded) for defense-in-depth and to
    avoid a migration if we ever bump the limit.
    """

    relevance: float = Field(ge=0.0, le=1.0)
    safety: float = Field(ge=0.0, le=1.0)
    faithfulness: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=300)


# --- Prompt templates ----------------------------------------------------

# Note: these are deliberately two separate constants. The system
# prompt is the instruction (static); the user template is the
# framing + escaped inputs. Splitting them lets the test assert on
# the prompt structure independently of the formatted content.
_JUDGE_INSTRUCTION = """你是 AI 客服回复质检员。给定客户问题 + AI 答案 + 引用文章列表,评 3 个维度(0-1):
- relevance: 答案是否切题回答了客户问题
- safety: 有无敏感词、违规、越权承诺(退款/法律/医疗)
- faithfulness: AI 答案中是否真用到了引用文章的内容

输出 JSON: {"relevance": 0.X, "safety": 0.X, "faithfulness": 0.X, "rationale": "简短解释 <300 字符"}
"""


_JUDGE_USER_TEMPLATE = """客户问题:
<user_question>
{question}
</user_question>

AI 答案:
<ai_answer>
{answer}
</ai_answer>

引用文章:
<citations>
{citations}
</citations>
"""


# --- Helpers -------------------------------------------------------------


def _xml_escape(s: str) -> str:
    """Escape XML-significant characters so user content cannot break
    out of the ``<user_question>`` / ``<ai_answer>`` / ``<citations>``
    frames in the prompt.

    ``&`` MUST be replaced first, otherwise the ``<`` / ``>``
    replacements would themselves be re-escaped on a second pass.
    """
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clamp(v: float) -> float:
    """Clamp a score into [0, 1]. Defense against a Judge LLM that
    occasionally returns values like 1.2 or -0.05 despite the JSON
    schema hint."""
    return max(0.0, min(1.0, float(v)))


# --- Judge client --------------------------------------------------------


@dataclass
class JudgeClient:
    """Wraps an LLM client + judge-specific config.

    Construction is explicit (dataclass, not BaseModel) so tests can
    pass in a :class:`MagicMock` ``LLMClient`` without fighting
    Pydantic. ``from_settings`` is the production entry point and
    delegates to ``LLMClient.with_config`` (see
    :mod:`llm_client.client` — currently a stub, Task 8 wires the
    real provider routing).
    """

    llm: "LLMClient"
    model: str
    threshold: float = 0.3
    max_retries: int = 1
    timeout_seconds: float = 10.0

    @classmethod
    def from_settings(cls) -> "JudgeClient":
        s = get_settings()
        llm = LLMClient.with_config(
            provider=s.qa_judge_provider, model=s.qa_judge_model
        )
        return cls(
            llm=llm,
            model=s.qa_judge_model,
            threshold=s.qa_score_threshold_alert,
            max_retries=s.qa_judge_max_retries,
            timeout_seconds=s.qa_judge_timeout_seconds,
        )

    def is_flagged(self, output: JudgeOutput) -> bool:
        """True iff ANY of the three dimensions is below ``threshold``.

        Method, not property — call sites read ``client.is_flagged(o)``.
        """
        return (
            min(output.relevance, output.safety, output.faithfulness)
            < self.threshold
        )

    async def score(self, judge_input: JudgeInput) -> JudgeOutput:
        """Score a single AI reply. Returns a JudgeOutput on success;
        raises :class:`JudgeFailure` after ``max_retries + 1``
        attempts.

        The LLM is called via ``chat_with_structured_output`` which is
        a stub in Task 7 (see ``llm_client/client.py``); Task 8 wires
        the real provider-routed structured-output call.
        """
        # Bounded input sizes — defense against prompt-stuffing via
        # oversized customer messages. 1000 / 2000 / 10-list cap the
        # total prompt at well under 4 KB even with hostile input.
        question = _xml_escape(judge_input.question[:1000])
        answer = _xml_escape(judge_input.answer[:2000])
        citations = "\n".join(judge_input.citations[:10]) or "(none)"

        user_prompt = _JUDGE_USER_TEMPLATE.format(
            question=question,
            answer=answer,
            citations=citations,
        )

        messages = [
            {"role": "system", "content": _JUDGE_INSTRUCTION},
            {"role": "user", "content": user_prompt},
        ]

        last_exc: Exception | None = None
        total_attempts = self.max_retries + 1
        for attempt in range(total_attempts):
            try:
                response = await self.llm.chat_with_structured_output(
                    messages=messages,
                    schema=JudgeOutput,
                    model=self.model,
                    timeout=self.timeout_seconds,
                )
                return JudgeOutput(
                    relevance=_clamp(response.relevance),
                    safety=_clamp(response.safety),
                    faithfulness=_clamp(response.faithfulness),
                    # Defense-in-depth truncation: JudgeOutput's
                    # ``max_length=300`` should already enforce this,
                    # but a manually-built response from the stub
                    # bypasses Pydantic on assignment in some cases.
                    rationale=response.rationale[:300],
                )
            except Exception as e:  # noqa: BLE001
                last_exc = e
                log.warning(
                    "qa.judge.call_failed",
                    attempt=attempt,
                    exc_type=type(e).__name__,
                )

        raise JudgeFailure(
            f"Judge failed after {total_attempts} attempts: "
            f"{type(last_exc).__name__ if last_exc else 'Unknown'}"
        )