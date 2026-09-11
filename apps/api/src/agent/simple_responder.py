"""M1 minimal AI auto-reply. Stage 7 wraps this around a LangGraph agent.

Public surface (``SimpleResponder.respond``) is unchanged from the
pre-LangGraph version so the conversation router and all existing
tests keep working. Internally, ``respond()`` now:

1. Fetches the conversation + recent messages via the existing
   :class:`ConversationService` calls.
2. Maps the ORM ``Message`` rows to LangChain ``BaseMessage`` and
   applies the existing history-trimming + summarization logic
   (kept in :meth:`SimpleResponder._build_messages`, renamed from
   ``_build_history`` because the return type is now
   ``list[BaseMessage]``).
3. Compiles the LangGraph agent graph (memoized on first call) and
   invokes it with the assembled state.
4. Wraps ``state["final_text"]`` into an ``AgentResponse``.

The graph itself is defined in :mod:`agent.graph.graph` —
``SimpleResponder`` is just the conversation-aware adapter that
bridges the existing DB layer to the LangGraph state schema.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from agent.graph.graph import build_agent_graph
from agent.graph.prompts import (
    _SUMMARIZE_SYSTEM_PROMPT,
    CHAT_MAX_TOKENS,
    CHAT_TEMPERATURE,
    FALLBACK_MESSAGE,
    FALLBACK_SUMMARY_CHARS,
    FALLBACK_TRANSCRIPT_CHARS,
    M1_SYSTEM_PROMPT,
    SUMMARY_MAX_TOKENS,
    SUMMARY_TEMPERATURE,
)
from agent.llm_factory import _default_llm_client_factory
from conversation.enums import MessageRole
from conversation.models import Message
from conversation.service import ConversationService
from knowledge.rag_service import RAGService
from llm_client.client import LLMClient
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


# Type alias for the per-tenant LLMClient factory. Stage 7+ may swap
# this for a config-driven resolver that picks model + provider per tenant.
LLMClientFactory = Callable[[str], LLMClient]

# Re-export the prompt / fallback constants from
# :mod:`agent.graph.prompts` for backward compatibility. The
# pre-Stage-7.1 public surface imported these directly from
# ``agent.simple_responder`` (see ``tests/agent/test_simple_responder.py``
# and any out-of-tree callers); keep that contract working.
__all__ = [
    "CHAT_MAX_TOKENS",
    "CHAT_TEMPERATURE",
    "DEFAULT_MODEL",
    "FALLBACK_MESSAGE",
    "FALLBACK_SUMMARY_CHARS",
    "FALLBACK_TRANSCRIPT_CHARS",
    "M1_SYSTEM_PROMPT",
    "MAX_HISTORY_BEFORE_SUMMARY",
    "MAX_HISTORY_MESSAGES",
    "SUMMARY_MAX_TOKENS",
    "SUMMARY_TEMPERATURE",
    "AgentResponse",
    "LLMClientFactory",
    "SimpleResponder",
]


@dataclass(frozen=True)
class AgentResponse:
    """Result of an AI turn. Caller persists the message."""

    content_text: str
    role: MessageRole  # always MessageRole.AI for now


class SimpleResponder:
    """Minimal M1 agent: single-turn LLM call with RAG context injection.

    Internally delegates to the LangGraph agent graph defined in
    :mod:`agent.graph.graph`. The graph is compiled lazily on the
    first :meth:`respond` call (or eagerly via :meth:`_ensure_graph`)
    and memoized for the responder's lifetime.
    """

    def __init__(
        self,
        *,
        conv_service: ConversationService | None = None,
        llm_client_factory: LLMClientFactory | None = None,
        rag_service: RAGService | None = None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self._conv_service = conv_service or ConversationService()
        self._llm_client_factory: LLMClientFactory = (
            llm_client_factory or _default_llm_client_factory
        )
        # Lazy RAG service so the SimpleResponder can be instantiated
        # in tests that don't need retrieval — the RAG service is only
        # touched when ``_build_messages`` runs.
        self._rag_service = rag_service or RAGService()
        self._model = model
        # Compiled LangGraph agent graph. Built lazily on first
        # ``respond()`` and reused thereafter. ``Any`` because
        # ``langgraph`` is not fully typed; the public surface we
        # touch is ``.ainvoke(state_dict)``.
        self._graph: Any | None = None

    def _ensure_graph(self) -> Any:
        """Compile the LangGraph agent graph on first use."""
        if self._graph is None:
            self._graph = build_agent_graph(
                rag_service=self._rag_service,
                llm_client_factory=self._llm_client_factory,
                model=self._model,
            )
        return self._graph

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

        On LLM failure, the graph's ``llm_node`` swallows the exception
        and writes ``FALLBACK_MESSAGE`` into ``state["final_text"]``, so
        the customer always gets *something*. Stage 7.2+ will replace
        this with proper escalation.
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

        messages = await self._build_messages(
            tenant_id=tenant_id, conversation_id=conversation_id
        )

        graph = self._ensure_graph()
        # ``ainvoke`` accepts a dict that conforms to ``AgentState``.
        # LangGraph fills any missing keys with ``None``; we pass all
        # of them explicitly so the TypedDict contract is met.
        result_state: dict[str, Any] = await graph.ainvoke(
            {
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "messages": messages,
                "rag_messages": [],
                "final_text": None,
            }
        )

        final_text = result_state.get("final_text")
        if not isinstance(final_text, str) or not final_text.strip():
            # The graph's llm_node already downgrades empty / failed
            # responses to ``FALLBACK_MESSAGE``, but defend against a
            # future graph change that yields ``None`` instead.
            logger.warning(
                "agent: graph returned empty final_text, sending fallback",
                extra={"conversation_id": conversation_id, "tenant_id": tenant_id},
            )
            return AgentResponse(
                content_text=FALLBACK_MESSAGE,
                role=MessageRole.AI,
            )

        return AgentResponse(
            content_text=final_text,
            role=MessageRole.AI,
        )

    async def _build_messages(
        self, *, tenant_id: str, conversation_id: str
    ) -> list[BaseMessage]:
        """Build the LangChain message list from the most recent messages.

        Maps our ``MessageRole`` (CUSTOMER/AGENT/AI/SYSTEM/TOOL) to
        LangChain's ``BaseMessage`` subclasses. TOOL payloads are
        skipped because the simple responder does not consume them.

        **RAG (Task 6.12).** When the conversation has at least one
        customer message, run :meth:`RAGService.build_context_for_query`
        against the most recent customer message and prepend the
        formatted chunk block as a synthetic ``SystemMessage`` at the
        start of the history. If retrieval yields no chunks (no KB,
        no hits, or retrieval error), the history is unchanged —
        backward-compatible with the no-RAG behaviour. (The graph's
        ``retrieve_node`` does the same RAG lookup independently;
        we *also* run it here so the summary prompt — when invoked —
        already has the same RAG context. The graph's lookup is the
        one used by the LLM.)

        When the conversation has more than ``MAX_HISTORY_BEFORE_SUMMARY``
        messages, the oldest overflowing messages are summarized via a
        separate LLM call and the summary is prepended as a
        ``SystemMessage``. The latest ``MAX_HISTORY_MESSAGES`` are
        kept verbatim. Stage 7+ should persist the summary on the
        conversation rather than regenerating it every turn.

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

        # ---- RAG context injection (Task 6.12) ----------------------
        # Run RAG against the most recent CUSTOMER message (the same
        # message the customer just sent that triggered this turn).
        # The RAG service is best-effort — any failure (no KB,
        # embedding error, KB not found) returns an empty RagContext
        # and we leave the history unchanged. This is the critical
        # backward-compat invariant: a tenant with no KB behaves
        # identically to a no-RAG build.
        rag_message = await self._build_rag_message(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            messages=msgs,
        )

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
                summary_message: BaseMessage = SystemMessage(
                    content=f"Previous conversation summary:\n{summary_text}"
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
                    f"{m.role}: {m.content_text}" for m in to_summarize
                )[:FALLBACK_TRANSCRIPT_CHARS]
                summary_message = SystemMessage(
                    content=f"Earlier conversation (truncated):\n{transcript}"
                )
            mapped = [self._map_message(m) for m in to_keep]
            return [
                *([rag_message] if rag_message is not None else []),
                summary_message,
                *[m for m in mapped if m is not None],
            ]

        # Short conversation: keep the LATEST MAX_HISTORY_MESSAGES verbatim.
        # (Defensive slice in case the repository ignored `limit`.)
        kept = msgs[-MAX_HISTORY_MESSAGES:]
        mapped = [self._map_message(m) for m in kept]
        tail = [m for m in mapped if m is not None]
        if rag_message is not None:
            return [rag_message, *tail]
        return tail

    def _map_message(self, m: Message) -> BaseMessage | None:
        """Map our ``Message`` ORM to a LangChain ``BaseMessage``.

        Returns ``None`` for roles we do not forward to the LLM
        (currently only ``TOOL``).
        """
        if m.role == MessageRole.CUSTOMER:
            return HumanMessage(content=m.content_text)
        if m.role in (MessageRole.AGENT, MessageRole.AI):
            return AIMessage(content=m.content_text)
        if m.role == MessageRole.SYSTEM:
            return SystemMessage(content=m.content_text)
        # TOOL — skipped; not consumed by the simple responder
        return None

    async def _summarize_history(
        self, *, tenant_id: str, messages: list[Message]
    ) -> str:
        """Use the LLM to summarize the oldest messages into 2-3 sentences.

        On LLM failure, returns a truncated transcript as a fallback so
        the caller still has *some* context to work with.
        """
        from llm_client.types import ChatMessage as LLMChatMessage

        transcript = "\n".join(
            f"{m.role}: {m.content_text}" for m in messages
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

    async def _build_rag_message(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        messages: list[Message],
    ) -> BaseMessage | None:
        """Build a synthetic RAG system message, or return ``None``.

        Looks for the most recent customer message in ``messages``;
        if none exists (empty history, only AI / agent messages),
        returns ``None`` and the LLM gets a no-RAG history — this
        matches the pre-6.12 behavior for the "agent-continued
        without a customer turn" path.

        Returns ``None`` if the RAG service yields no chunks (no
        KB for the tenant, no hits, or any retrieval error). The
        RAG service is fully exception-safe — we additionally
        catch any unexpected error here so a future regression in
        the RAG layer never takes down the AI auto-reply.

        The returned message, when present, is a synthetic ``system``
        turn. It is NOT persisted to the messages table — it lives
        only inside the LLM request payload.
        """
        # Walk from the END — the most recent customer message is
        # usually the last one. ``reversed`` so we short-circuit on
        # the freshest customer message rather than the oldest.
        latest_customer: Message | None = next(
            (m for m in reversed(messages) if m.role == MessageRole.CUSTOMER),
            None,
        )
        if latest_customer is None or not latest_customer.content_text:
            return None

        try:
            rag_context = await self._rag_service.build_context_for_query(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                query=latest_customer.content_text,
            )
        except Exception:
            # Defence-in-depth: the RAG service itself catches
            # everything, but we don't want a future regression to
            # take down the AI auto-reply. Log + fall through to
            # no-RAG behavior.
            logger.warning(
                "agent: RAG service raised unexpectedly, continuing without RAG",
                extra={
                    "tenant_id": tenant_id,
                    "conversation_id": conversation_id,
                },
                exc_info=True,
            )
            return None

        if rag_context.chunk_count == 0 or not rag_context.system_message:
            return None

        return SystemMessage(content=rag_context.system_message)
