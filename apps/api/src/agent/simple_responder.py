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

M1_SYSTEM_PROMPT = """You are a friendly customer-service agent for an AI-customer platform.
Answer the customer's question concisely. If you don't know, say so honestly
and suggest escalating to a human agent. Reply in the customer's language."""

FALLBACK_MESSAGE = "抱歉,AI 助手暂时无法回复,请稍后再试或联系人工客服。"


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
                extra={"conversation_id": conversation_id},
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
                temperature=0.7,
                max_tokens=512,
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

        The history is defensively capped at ``MAX_HISTORY_MESSAGES``
        so a misbehaving repository (e.g. one that ignores ``limit``)
        cannot blow up the prompt.
        """
        msgs = await self._conv_service.list_messages(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            limit=MAX_HISTORY_MESSAGES,
        )
        # Defensive slice in case the repository ignored `limit` and returned more —
        # keep the LATEST MAX_HISTORY_MESSAGES, since recency matters most for the LLM context.
        msgs = (msgs or [])[-MAX_HISTORY_MESSAGES:]
        out: list[LLMChatMessage] = []
        for m in msgs:
            if m.role == MessageRole.CUSTOMER:
                out.append(
                    LLMChatMessage(role=LLMMessageRole.USER, content=m.content_text)
                )
            elif m.role in (MessageRole.AGENT, MessageRole.AI):
                out.append(
                    LLMChatMessage(
                        role=LLMMessageRole.ASSISTANT, content=m.content_text
                    )
                )
            elif m.role == MessageRole.SYSTEM:
                out.append(
                    LLMChatMessage(role=LLMMessageRole.SYSTEM, content=m.content_text)
                )
            # TOOL — skipped; not consumed by the simple responder
        return out