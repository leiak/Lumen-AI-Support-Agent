"""AI-suggested reply service for the agent workspace (Stage 8.3).

Exposes :class:`SuggestionService.suggest_reply`, the read-only
"show me what the AI would say right now" preview invoked by
``POST /api/v1/agents/conversations/{id}/suggest-reply``.

Design constraints
------------------

* **READ-ONLY.** This module never persists anything to the DB
  and never mutates conversation state. It does not call
  :meth:`ConversationService.record_message`, does not dispatch
  the ``escalate_to_human`` tool, and does not broadcast WS
  events. Tests in ``test_agent_suggest.py`` pin this invariant
  with explicit "does not persist / dispatch / broadcast"
  assertions.

* **PII-safe logs.** All log payloads use kwargs-only
  structlog and contain only opaque IDs (tenant_id,
  conversation_id) and ``error_type=type(exc).__name__``. No
  customer text, no chunk text, no email, no exception repr.

* **RAG fail-open.** Any error in the RAG path (embedding
  failure, KB lookup error, retrieval exception) is downgraded
  to an empty :class:`RagContext`. The LLM still receives a
  request with just the conversation history; ``turn_kind`` is
  set to ``"no_rag"``.

* **LLM fail-safe.** Any error in the LLM call (rate-limit,
  provider unavailable, etc.) is downgraded to
  :data:`FALLBACK_MESSAGE`. ``turn_kind`` is set to
  ``"llm_unavailable"`` and ``warning`` carries
  ``"llm_unavailable"`` so the frontend can render a retry
  affordance.

* **Tool calls are ignored.** Even if the LLM returns
  ``tool_calls``, the suggestion service ignores them. The
  endpoint is a preview; dispatching tools would mutate state
  behind the agent's back.

Anti-enumeration: ``SuggestionServiceNotFoundError`` is raised
on cross-tenant / unknown ``conversation_id`` so the API layer
can map both to a single 404 response with the same wording
the rest of the workspace uses.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent.exceptions import SuggestionServiceNotFoundError
from agent.graph.prompts import (
    CHAT_MAX_TOKENS,
    CHAT_TEMPERATURE,
    FALLBACK_MESSAGE,
    M1_SYSTEM_PROMPT,
)
from agent.llm_factory import _default_llm_client_factory, _resolve_default_model
from agent.schemas import CITATION_TEXT_MAX_CHARS, CitationOut
from agent.simple_responder import MAX_HISTORY_MESSAGES
from conversation.enums import MessageRole
from conversation.service import ConversationService
from core.logging import get_logger
from knowledge.rag_service import RAGService
from knowledge.retriever import RetrievedChunk, retrieve_chunks
from llm_client.client import LLMClient
from llm_client.exceptions import (
    InvalidRequest,
    OutputInvalid,
    ProviderUnavailable,
    RateLimited,
)
from llm_client.types import (
    ChatMessage as LLMChatMessage,
)
from llm_client.types import (
    ChatRequest,
)
from llm_client.types import (
    MessageRole as LLMMessageRole,
)

log = get_logger(__name__)

# Warning tag returned on the ``SuggestionOut.warning`` field when
# the LLM call fails. The frontend uses this exact string to
# render a retry button — keep it stable across versions.
LLM_UNAVAILABLE_WARNING = "llm_unavailable"


@dataclass(frozen=True)
class SuggestionResult:
    """Internal result of :meth:`SuggestionService.suggest_reply`.

    Mirrors :class:`agent.schemas.SuggestionOut` but holds raw
    dataclass fields so the service can stay decoupled from the
    HTTP shape. The route handler maps this into the Pydantic
    response.

    ``citations`` carries raw :class:`CitationOut` entries with
    text already truncated to
    :data:`agent.schemas.CITATION_TEXT_MAX_CHARS`.
    """

    suggested_text: str
    citations: list[CitationOut]
    retrieval_score_max: float
    warning: str | None
    turn_kind: str
    # Tenant + conversation id are echoed back so the route
    # handler can populate ``SuggestionOut.conversation_id``
    # without re-fetching the conversation.
    tenant_id: str
    conversation_id: str


# Type alias for the per-tenant LLMClient factory. Mirrors the
# one declared in ``agent.simple_responder`` — duplicated here
# to avoid a re-import (and the implicit cycle that would
# require). The shape is identical and stable.
LLMClientFactory = Callable[[str], LLMClient]


def _truncate_citation_text(text: str, *, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars, appending an ellipsis.

    A hard slice (not word-boundary) keeps the output
    deterministic so the frontend can rely on the truncation
    marker as a "this is a preview" hint.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _latest_customer_text(messages: Any) -> str | None:
    """Return the text of the most recent CUSTOMER message.

    Walks the list in reverse so we short-circuit on the freshest
    customer turn. Returns ``None`` when no customer message
    exists — the caller treats that as the
    ``"no_customer_message"`` turn kind.
    """
    for msg in reversed(messages):
        if msg.role == MessageRole.CUSTOMER:
            text = msg.content_text
            if isinstance(text, str) and text.strip():
                return text
    return None


class SuggestionService:
    """Stateless AI-suggested reply generator for the agent workspace.

    The service is constructed once per request (or held as a
    long-lived instance — it's safe to share, there is no
    per-instance state). All read paths are tenant-scoped via
    the explicit ``tenant_id`` parameter.

    Parameters
    ----------
    conv_service:
        The conversation service used to look up the conversation
        and its messages. Defaults to a real
        :class:`ConversationService` instance — production code
        never has to pass one explicitly.
    llm_client_factory:
        Per-tenant ``LLMClient`` factory. Defaults to
        :func:`agent.llm_factory._default_llm_client_factory`.
    rag_service:
        The RAG service used for retrieval-augmented context
        injection. Defaults to a real :class:`RAGService`.
    model:
        Model identifier forwarded to ``LLMClient.chat``.
        Defaults to :data:`agent.simple_responder.DEFAULT_MODEL`.
    """

    def __init__(
        self,
        *,
        conv_service: ConversationService | None = None,
        llm_client_factory: LLMClientFactory | None = None,
        rag_service: RAGService | None = None,
        model: str | None = None,
    ) -> None:
        self._conv_service = conv_service or ConversationService()
        self._llm_client_factory: LLMClientFactory = (
            llm_client_factory or _default_llm_client_factory
        )
        self._rag_service = rag_service or RAGService()
        self._model = model or _resolve_default_model()

    async def suggest_reply(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
    ) -> SuggestionResult:
        """Generate a read-only AI-suggested reply for the conversation.

        Steps:

        1. Look up the conversation via
           :meth:`ConversationService.get` — cross-tenant /
           unknown ULIDs raise :class:`SuggestionServiceNotFoundError`.
        2. Load the latest ``MAX_HISTORY_MESSAGES`` messages via
           :meth:`ConversationService.list_messages`.
        3. Find the latest customer message. If none exists →
           return an empty :class:`SuggestionResult` with
           ``turn_kind="no_customer_message"``.
        4. Run RAG via
           :meth:`RAGService.build_context_for_query`. The
           service is fail-open; an empty :class:`RagContext`
           becomes ``turn_kind="no_rag"`` with no citations.
        5. Re-run the retriever directly to get the
           :class:`RetrievedChunk` objects so we can return
           ``(article_id, chunk_index, text, score)`` citations.
        6. Assemble ``[M1_SYSTEM_PROMPT, rag_system, *history]``
           and call :meth:`LLMClient.chat`.
        7. On LLM failure → ``turn_kind="llm_unavailable"``,
           ``suggested_text=FALLBACK_MESSAGE``,
           ``warning="llm_unavailable"``.

        The function NEVER mutates DB state, NEVER dispatches
        tools, NEVER broadcasts WS events.
        """
        # ---- Step 1: lookup conversation -----------------------------
        conv = await self._conv_service.get(
            tenant_id=tenant_id, conversation_id=conversation_id
        )
        if conv is None:
            log.info(
                "agent.suggest.conv_not_found",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )
            raise SuggestionServiceNotFoundError()

        # ---- Step 2: load message history ----------------------------
        msgs = await self._conv_service.list_messages(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            limit=MAX_HISTORY_MESSAGES,
        )
        msgs = msgs or []

        # ---- Step 3: find the latest customer turn -------------------
        last_customer_text = _latest_customer_text(msgs)
        if last_customer_text is None:
            log.info(
                "agent.suggest.no_customer_message",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )
            return SuggestionResult(
                suggested_text="",
                citations=[],
                retrieval_score_max=0.0,
                warning=None,
                turn_kind="no_customer_message",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )

        # ---- Step 4: RAG (fail-open) ---------------------------------
        rag_context = await self._rag_service.build_context_for_query(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            query=last_customer_text,
        )
        citations: list[CitationOut] = []
        retrieval_score_max = rag_context.retrieval_score_max
        rag_block = ""

        # ---- Step 5: hydrate citations from the retriever -----------
        # ``build_context_for_query`` swallows retrieval failures
        # into an empty ``RagContext`` so we know that a non-empty
        # ``chunk_count`` implies the underlying retrieval
        # actually succeeded. We re-run the retriever to obtain
        # the :class:`RetrievedChunk` objects (the RAG service's
        # public surface only exposes the formatted text, not
        # the underlying chunk metadata).
        if rag_context.chunk_count > 0 and rag_context.knowledge_base_id:
            try:
                raw_chunks = await retrieve_chunks(
                    tenant_id=tenant_id,
                    knowledge_base_id=rag_context.knowledge_base_id,
                    query=last_customer_text,
                    top_k=RAGService.DEFAULT_TOP_K,
                    score_threshold=RAGService.DEFAULT_SCORE_THRESHOLD,
                )
            except Exception as exc:
                # Defence-in-depth — ``RAGService.build_context_for_query``
                # already swallows retrieval failures, so this branch
                # should be unreachable. If a regression sneaks in we
                # downgrade to no-citations rather than failing the
                # suggest endpoint. PII-safe payload.
                log.warning(
                    "agent.suggest.rag_citation_lookup_failed",
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    error_type=type(exc).__name__,
                )
                raw_chunks = []

            citations = _build_citations(raw_chunks)
            retrieval_score_max = max(
                (c.score for c in citations), default=0.0
            )

        # Use the formatted RAG block the RAG service already
        # produced. An empty block (no-RAG path) is fine — the
        # LLM still gets the conversation history.
        if rag_context.chunk_count > 0 and rag_context.system_message:
            rag_block = rag_context.system_message

        # ---- Step 6: assemble the LLM request ------------------------
        llm_messages = _build_llm_messages(
            rag_block=rag_block,
            history=msgs,
        )

        # ---- Step 7: call the LLM (fail-safe) ------------------------
        try:
            client = self._llm_client_factory(tenant_id)
            request = ChatRequest(
                model=self._model,
                messages=llm_messages,
                temperature=CHAT_TEMPERATURE,
                max_tokens=CHAT_MAX_TOKENS,
            )
            response = await client.chat(request)
        except (
            RateLimited,
            ProviderUnavailable,
            OutputInvalid,
            InvalidRequest,
        ) as exc:
            # Typed catch — these are the LLM client's documented
            # error modes. Logged at WARNING with class name only;
            # we never include exception repr (can carry API hints).
            log.warning(
                "agent.suggest.llm_failed",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                error_type=type(exc).__name__,
            )
            return SuggestionResult(
                suggested_text=FALLBACK_MESSAGE,
                citations=citations,
                retrieval_score_max=retrieval_score_max,
                warning=LLM_UNAVAILABLE_WARNING,
                turn_kind="llm_unavailable",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )
        except Exception as exc:
            # Defence-in-depth — the typed catch above covers every
            # documented LLM error mode, but if a regression sneaks
            # in we refuse to 500 the suggest endpoint. PII-safe.
            log.warning(
                "agent.suggest.llm_failed_unexpected",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                error_type=type(exc).__name__,
            )
            return SuggestionResult(
                suggested_text=FALLBACK_MESSAGE,
                citations=citations,
                retrieval_score_max=retrieval_score_max,
                warning=LLM_UNAVAILABLE_WARNING,
                turn_kind="llm_unavailable",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )

        # Empty LLM content is treated as a failure too — a
        # "blank" suggestion is worse than the explicit fallback
        # because the agent can't tell whether the model had
        # nothing to say or the response was truncated.
        text = getattr(response, "content", None)
        if not isinstance(text, str) or not text.strip():
            log.warning(
                "agent.suggest.llm_empty",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )
            return SuggestionResult(
                suggested_text=FALLBACK_MESSAGE,
                citations=citations,
                retrieval_score_max=retrieval_score_max,
                warning=LLM_UNAVAILABLE_WARNING,
                turn_kind="llm_unavailable",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )

        # The endpoint ignores any tool_calls the LLM returned.
        # The agent-facing preview is a text-only suggestion;
        # mutating state behind the agent's back via tool dispatch
        # would be a silent side-effect.
        turn_kind = "rag_hit" if rag_context.chunk_count > 0 else "no_rag"

        return SuggestionResult(
            suggested_text=text,
            citations=citations,
            retrieval_score_max=retrieval_score_max,
            warning=None,
            turn_kind=turn_kind,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )


def _build_citations(raw_chunks: list[RetrievedChunk]) -> list[CitationOut]:
    """Map retriever output to the citation shape surfaced on the API.

    Truncates the chunk text to
    :data:`agent.schemas.CITATION_TEXT_MAX_CHARS` so the
    suggestion payload stays compact. Preserves the retriever's
    native highest-score-first ordering so the frontend can
    render citations in priority order without re-sorting.
    """
    out: list[CitationOut] = []
    for rc in raw_chunks:
        out.append(
            CitationOut(
                article_id=rc.article_id,
                chunk_index=rc.chunk.chunk_index,
                text=_truncate_citation_text(
                    rc.text, limit=CITATION_TEXT_MAX_CHARS
                ),
                score=float(rc.score),
            )
        )
    return out


def _build_llm_messages(
    *,
    rag_block: str,
    history: Any,
) -> list[LLMChatMessage]:
    """Assemble the ``LLMChatMessage`` list for the chat request.

    Order:

    1. ``M1_SYSTEM_PROMPT`` — the agent's role / tone.
    2. RAG block as an additional ``SYSTEM`` message (omitted
       when empty — a no-RAG path doesn't need the slot).
    3. The latest ``MAX_HISTORY_MESSAGES`` history messages,
       mapped to LLM roles: ``CUSTOMER → USER``, ``AGENT /
       AI → ASSISTANT``, ``SYSTEM → SYSTEM``. ``TOOL`` rows
       are skipped because the LLM client doesn't accept them
       and we never want to feed raw tool output back through
       the agent preview.

    The history mapping mirrors
    :meth:`agent.simple_responder.SimpleResponder._map_message`
    minus the summary path — the suggestion endpoint keeps the
    latest messages verbatim so the preview reflects what the
    agent sees in the conversation timeline.
    """
    messages: list[LLMChatMessage] = [
        LLMChatMessage(role=LLMMessageRole.SYSTEM, content=M1_SYSTEM_PROMPT),
    ]
    if rag_block:
        messages.append(
            LLMChatMessage(role=LLMMessageRole.SYSTEM, content=rag_block)
        )
    for msg in history:
        mapped = _map_history_message(msg)
        if mapped is not None:
            messages.append(mapped)
    return messages


def _map_history_message(msg: Any) -> LLMChatMessage | None:
    """Map a domain ``Message`` to an ``LLMChatMessage``.

    Mirrors :meth:`SimpleResponder._map_message` so the agent's
    preview sees the same role encoding the production
    :class:`SimpleResponder` would send. ``TOOL`` rows are
    skipped.
    """
    if msg.role == MessageRole.CUSTOMER:
        return LLMChatMessage(role=LLMMessageRole.USER, content=msg.content_text)
    if msg.role in (MessageRole.AGENT, MessageRole.AI):
        return LLMChatMessage(
            role=LLMMessageRole.ASSISTANT, content=msg.content_text
        )
    if msg.role == MessageRole.SYSTEM:
        return LLMChatMessage(
            role=LLMMessageRole.SYSTEM, content=msg.content_text
        )
    # TOOL — skipped; the LLM client doesn't accept tool rows
    # in the request messages.
    return None


__all__ = [
    "LLM_UNAVAILABLE_WARNING",
    "LLMClientFactory",
    "SuggestionResult",
    "SuggestionService",
]
