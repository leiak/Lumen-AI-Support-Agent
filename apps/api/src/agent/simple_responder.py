"""M1 minimal AI auto-reply. Stage 7 will replace this with a LangGraph agent.

Builds a chat history from the most recent messages in the conversation
and calls ``LLMClient.chat()`` with a hardcoded system prompt. The
response is returned as an ``AgentResponse`` for the caller to persist.

Design constraints:
- Synchronous-ish (no background queue in Stage 5).
- Single LLM call per customer message.
- No tools, no RAG (those are Stage 6 / 7).
- The interface is small enough that Stage 7's LangGraph implementer
  can ship a drop-in replacement.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from agent.llm_factory import _default_llm_client_factory
from conversation.enums import MessageRole
from conversation.models import Message
from conversation.service import ConversationService
from llm_client.client import LLMClient
from llm_client.types import ChatMessage as LLMChatMessage
from llm_client.types import ChatRequest
from llm_client.types import MessageRole as LLMMessageRole

logger = logging.getLogger(__name__)

# M1 default — Claude Haiku for low cost. Stage 7+ may make this
# per-tenant configurable.
DEFAULT_MODEL = "claude-haiku-4-5"
MAX_HISTORY_MESSAGES = 20  # keep prompts bounded
# Only summarize when the conversation has more than this many messages.
# Below this threshold, the kept window already covers all context. Above it,
# the overflowing oldest messages get collapsed into a single summary message
# prepended to the chat. Stage 7+ should persist summaries on the conversation
# rather than regenerating each turn.
MAX_HISTORY_BEFORE_SUMMARY = 50

M1_SYSTEM_PROMPT = """You are a friendly customer-service agent for an AI-customer platform.
Answer the customer's question concisely. If you don't know, say so honestly
and suggest escalating to a human agent. Reply in the customer's language."""

_SUMMARIZE_SYSTEM_PROMPT = (
    "You are a helpful assistant. Summarize the following customer service "
    "conversation in 2-3 sentences. Preserve customer questions, key facts "
    "(order numbers, products, etc.), and the current state of the issue. "
    "Be concise."
)

FALLBACK_MESSAGE = "抱歉,AI 助手暂时无法回复,请稍后再试或联系人工客服。"
CHAT_TEMPERATURE = 0.7
CHAT_MAX_TOKENS = 512
SUMMARY_TEMPERATURE = 0.3
SUMMARY_MAX_TOKENS = 200
FALLBACK_TRANSCRIPT_CHARS = 1000
FALLBACK_SUMMARY_CHARS = 500


# Type alias for the per-tenant LLMClient factory. Stage 7+ may swap
# this for a config-driven resolver that picks model + provider per tenant.
LLMClientFactory = Callable[[str], LLMClient]


@dataclass(frozen=True)
class AgentResponse:
    """Result of an AI turn. Caller persists the message."""

    content_text: str
    role: MessageRole  # always MessageRole.AI for now


class SimpleResponder:
    """Minimal M1 agent: single-turn LLM call, no tools, no RAG."""

    def __init__(
        self,
        *,
        conv_service: ConversationService | None = None,
        llm_client_factory: LLMClientFactory | None = None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self._conv_service = conv_service or ConversationService()
        self._llm_client_factory: LLMClientFactory = (
            llm_client_factory or _default_llm_client_factory
        )
        self._model = model

    async def respond(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
    ) -> AgentResponse | None:
        """Generate an AI reply for the given conversation.

        Returns ``None`` if the conversation is not in AI-handling state
        (e.g., it was transferred to a human agent, or closed). Caller
        should NOT persist anything in that case.

        On LLM failure, logs a WARNING and returns a generic fallback
        response (so the customer gets *something*). Stage 7+ will
        replace this with proper escalation.
        """
        conv = await self._conv_service.get(
            tenant_id=tenant_id, conversation_id=conversation_id
        )
        if conv is None:
            logger.warning(
                "agent: conversation not found",
                extra={"tenant_id": tenant_id, "conversation_id": conversation_id},
            )
            return None
        if not conv.ai_handling:
            logger.info(
                "agent: conversation not in AI handling, skipping",
                extra={"tenant_id": tenant_id, "conversation_id": conversation_id},
            )
            return None

        history = await self._build_history(
            tenant_id=tenant_id, conversation_id=conversation_id
        )
        try:
            client = self._llm_client_factory(tenant_id)
            request = ChatRequest(
                model=self._model,
                messages=[
                    LLMChatMessage(
                        role=LLMMessageRole.SYSTEM, content=M1_SYSTEM_PROMPT
                    ),
                    *history,
                ],
                temperature=CHAT_TEMPERATURE,
                max_tokens=CHAT_MAX_TOKENS,
            )
            response = await client.chat(request)
            if not response.content.strip():
                logger.warning(
                    "agent: LLM returned empty content, sending fallback",
                    extra={"conversation_id": conversation_id, "tenant_id": tenant_id},
                )
                return AgentResponse(
                    content_text=FALLBACK_MESSAGE,
                    role=MessageRole.AI,
                )
            return AgentResponse(
                content_text=response.content,
                role=MessageRole.AI,
            )
        except Exception:
            logger.warning(
                "agent: LLM call failed, sending fallback",
                extra={"conversation_id": conversation_id, "tenant_id": tenant_id},
                exc_info=True,
            )
            return AgentResponse(
                content_text=FALLBACK_MESSAGE,
                role=MessageRole.AI,
            )

    async def _build_history(
        self, *, tenant_id: str, conversation_id: str
    ) -> list[LLMChatMessage]:
        """Build the LLM chat history from the most recent messages.

        Maps our ``MessageRole`` (CUSTOMER/AGENT/AI/SYSTEM/TOOL) to the
        LLM client's role vocabulary (user/assistant/system). TOOL
        payloads are skipped because the simple responder does not
        consume them.

        When the conversation has more than ``MAX_HISTORY_BEFORE_SUMMARY``
        messages, the oldest overflowing messages are summarized via a
        separate LLM call and the summary is prepended as a ``system``
        message. The latest ``MAX_HISTORY_MESSAGES`` are kept verbatim.
        Stage 7+ should persist the summary on the conversation rather
        than regenerating it every turn.

        The history is defensively capped at ``MAX_HISTORY_MESSAGES +
        MAX_HISTORY_BEFORE_SUMMARY + 1`` so a misbehaving repository
        (e.g. one that ignores ``limit``) cannot blow up the prompt.
        """
        fetch_limit = MAX_HISTORY_MESSAGES + MAX_HISTORY_BEFORE_SUMMARY + 1
        msgs = await self._conv_service.list_messages(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            limit=fetch_limit,
        )
        msgs = msgs or []

        if len(msgs) > MAX_HISTORY_BEFORE_SUMMARY:
            # Conversation exceeds the no-summarize threshold.
            # Summarize the oldest overflowing messages and keep the
            # latest MAX_HISTORY_MESSAGES verbatim.
            to_summarize = msgs[:-MAX_HISTORY_MESSAGES]
            to_keep = msgs[-MAX_HISTORY_MESSAGES:]
            try:
                summary_text = await self._summarize_history(
                    tenant_id=tenant_id, messages=to_summarize
                )
                summary_message = LLMChatMessage(
                    role=LLMMessageRole.SYSTEM,
                    content=f"Previous conversation summary:\n{summary_text}",
                )
            except Exception:
                logger.warning(
                    "agent: history summary failed, using truncated transcript",
                    extra={
                        "conversation_id": conversation_id,
                        "tenant_id": tenant_id,
                    },
                    exc_info=True,
                )
                transcript = "\n".join(
                    f"{m.role.value}: {m.content_text}" for m in to_summarize
                )[:FALLBACK_TRANSCRIPT_CHARS]
                summary_message = LLMChatMessage(
                    role=LLMMessageRole.SYSTEM,
                    content=f"Earlier conversation (truncated):\n{transcript}",
                )
            mapped = [self._map_message(m) for m in to_keep]
            return [summary_message, *[m for m in mapped if m is not None]]

        # Short conversation: keep the LATEST MAX_HISTORY_MESSAGES verbatim.
        # (Defensive slice in case the repository ignored `limit`.)
        kept = msgs[-MAX_HISTORY_MESSAGES:]
        mapped = [self._map_message(m) for m in kept]
        return [m for m in mapped if m is not None]

    def _map_message(self, m: Message) -> LLMChatMessage | None:
        """Map our ``Message`` ORM to an LLM ``ChatMessage``.

        Returns ``None`` for roles we do not forward to the LLM
        (currently only ``TOOL``).
        """
        if m.role == MessageRole.CUSTOMER:
            return LLMChatMessage(role=LLMMessageRole.USER, content=m.content_text)
        if m.role in (MessageRole.AGENT, MessageRole.AI):
            return LLMChatMessage(
                role=LLMMessageRole.ASSISTANT, content=m.content_text
            )
        if m.role == MessageRole.SYSTEM:
            return LLMChatMessage(
                role=LLMMessageRole.SYSTEM, content=m.content_text
            )
        # TOOL — skipped; not consumed by the simple responder
        return None

    async def _summarize_history(
        self, *, tenant_id: str, messages: list[Message]
    ) -> str:
        """Use the LLM to summarize the oldest messages into 2-3 sentences.

        On LLM failure, returns a truncated transcript as a fallback so
        the caller still has *some* context to work with.
        """
        transcript = "\n".join(
            f"{m.role.value}: {m.content_text}" for m in messages
        )
        client = self._llm_client_factory(tenant_id)
        request = ChatRequest(
            model=self._model,
            messages=[
                LLMChatMessage(
                    role=LLMMessageRole.SYSTEM, content=_SUMMARIZE_SYSTEM_PROMPT
                ),
                LLMChatMessage(role=LLMMessageRole.USER, content=transcript),
            ],
            temperature=SUMMARY_TEMPERATURE,
            max_tokens=SUMMARY_MAX_TOKENS,
        )
        try:
            response = await client.chat(request)
        except Exception:
            logger.warning(
                "agent: summary LLM call failed, using truncated transcript",
                extra={"tenant_id": tenant_id},
                exc_info=True,
            )
            return transcript[:FALLBACK_SUMMARY_CHARS]
        summary = response.content.strip()
        if not summary:
            return transcript[:FALLBACK_SUMMARY_CHARS]
        return summary