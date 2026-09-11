"""Prompt + fallback constants shared by the agent graph and the
:class:`SimpleResponder` adapter.

Lives in :mod:`agent.graph.prompts` (rather than inside
:mod:`agent.simple_responder`) so :mod:`agent.graph.nodes` can
import the values without re-entering ``agent.simple_responder``
and triggering a circular import.

Backward-compat
---------------

:mod:`agent.simple_responder` re-exports the public constants
(``M1_SYSTEM_PROMPT``, ``FALLBACK_MESSAGE``, ``_SUMMARIZE_SYSTEM_PROMPT``,
``SUMMARY_TEMPERATURE``, ``SUMMARY_MAX_TOKENS``, ``FALLBACK_TRANSCRIPT_CHARS``,
``FALLBACK_SUMMARY_CHARS``) so the existing test suite and any
out-of-tree imports keep working.
"""
from __future__ import annotations

# M1 system prompt — Claude Haiku's default behaviour. The graph and the
# pre-graph responder both inject this as the first message in the
# ``chat`` request.
M1_SYSTEM_PROMPT = """You are a friendly customer-service agent for an AI-customer platform.
Answer the customer's question concisely. If you don't know, say so honestly
and suggest escalating to a human agent. Reply in the customer's language."""

# Fallback text returned when the LLM call fails or returns empty.
FALLBACK_MESSAGE = "抱歉,AI 助手暂时无法回复,请稍后再试或联系人工客服。"

# Summarization prompt — used by ``SimpleResponder._summarize_history`` to
# collapse overflowing conversation history into a single SystemMessage.
_SUMMARIZE_SYSTEM_PROMPT = (
    "You are a helpful assistant. Summarize the following customer service "
    "conversation in 2-3 sentences. Preserve customer questions, key facts "
    "(order numbers, products, etc.), and the current state of the issue. "
    "Be concise."
)

# LLM request tuning. The chat temperature/max_tokens apply to both the
# graph's llm_node and any direct ``client.chat`` call from the
# SimpleResponder.
CHAT_TEMPERATURE = 0.7
CHAT_MAX_TOKENS = 512
SUMMARY_TEMPERATURE = 0.3
SUMMARY_MAX_TOKENS = 200

# How many characters of a failed transcript to keep when the summary
# LLM call throws. Used by the ``_summarize_history`` fallback path.
FALLBACK_TRANSCRIPT_CHARS = 1000
FALLBACK_SUMMARY_CHARS = 500


__all__ = [
    "CHAT_MAX_TOKENS",
    "CHAT_TEMPERATURE",
    "FALLBACK_MESSAGE",
    "FALLBACK_SUMMARY_CHARS",
    "FALLBACK_TRANSCRIPT_CHARS",
    "M1_SYSTEM_PROMPT",
    "SUMMARY_MAX_TOKENS",
    "SUMMARY_TEMPERATURE",
    "_SUMMARIZE_SYSTEM_PROMPT",
]
