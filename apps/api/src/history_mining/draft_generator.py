"""Generate KB draft titles + bodies from cluster representative questions.

Uses LLM with structured output (Pydantic schema).

PII discipline: never logs the raw questions; on LLM failure, fallback
draft contains the raw questions as body (acceptable since they are
the user's input to this pipeline) but the log line only records the
cluster size + error class.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class KBDraft(BaseModel):
    title: str = Field(..., max_length=200)
    body: str = Field(..., max_length=2000)
    suggested_tags: list[str] = Field(default_factory=list, max_length=10)


class KBDraftGenerator:
    def __init__(
        self,
        *,
        llm_client_factory: Callable[[], Any],
        max_questions: int = 5,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._llm_factory = llm_client_factory
        self._max_q = max_questions
        self._timeout = timeout_seconds

    async def generate(
        self,
        representative_questions: list[str],
        *,
        llm_client: Any | None = None,
        max_questions: int | None = None,
    ) -> KBDraft:
        """Generate a KB draft from a cluster of representative questions.

        On LLM failure, returns a fallback draft with placeholder title
        + raw questions as body so the cluster is never lost.
        """
        if llm_client is None:
            llm_client = self._llm_factory()

        cap = max_questions if max_questions is not None else self._max_q
        questions = representative_questions[:cap]
        questions_block = "\n".join(f"- {q}" for q in questions)

        prompt = (
            "Based on these customer questions, generate a KB article draft.\n"
            "Title: short, descriptive (max 200 chars).\n"
            "Body: a brief summary answering the common question (max 2000 chars).\n"
            "Tags: 1-3 lowercase tags.\n\n"
            f"Customer questions:\n{questions_block}\n\n"
            "Output JSON with keys: title, body, suggested_tags."
        )

        try:
            draft = await llm_client.chat_with_structured_output(
                messages=[
                    {
                        "role": "system",
                        "content": "You generate KB article drafts from customer question clusters.",
                    },
                    {"role": "user", "content": prompt},
                ],
                schema=KBDraft,
                model="gpt-4o-mini",  # Or read from settings; small model fine for this
                timeout=self._timeout,
            )
            return draft
        except Exception as e:
            logger.warning(
                "history_mining.draft_generation_failed",
                extra={
                    "error_type": type(e).__name__,
                    "n_questions": len(questions),
                },
            )
            # Graceful fallback — cluster is preserved with raw questions
            return KBDraft(
                title=f"Common question (cluster of {len(questions)})",
                body="\n".join(questions),
                suggested_tags=[],
            )