"""LangChain tools exposed by the M1 agent graph.

This module ships a single tool — ``escalate_to_human`` — for
Stage 7.2. Tools are returned by closure factories that bind
tenant-scoped state from the outer graph state so the LLM cannot
smuggle cross-tenant IDs through tool arguments.

Why a closure factory (not runtime injection)?
----------------------------------------------

M1 has one agent process per request and never re-enters the
graph; the per-tenant services do not change inside a turn.
Closing them over the factory keeps the tool surface minimal
and avoids dragging ``langchain.runtime.Runtime`` /
``InjectedState`` annotations into call sites. The trade-off
is that every graph run rebuilds the tool — cheap for M1.

PII contract
------------

The tool never logs ``reason`` / ``summary`` text. Only opaque
IDs and counts go into the structlog payload. The escalation
*message* (what the customer sees) is returned by the tool as
its result content — LangChain turns that into a ``ToolMessage``
the LLM never sees again.
"""
from __future__ import annotations

from typing import Any, cast

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from conversation.service import ConversationService
from core.logging import get_logger

log = get_logger(__name__)


class EscalateArgs(BaseModel):
    """Pydantic schema for the ``escalate_to_human`` tool arguments.

    LangChain's ``@tool`` decorator inspects ``args_schema`` to
    build the JSON Schema sent to the model. ``tenant_id`` and
    ``conversation_id`` are intentionally NOT in this schema —
    they are bound by the closure factory below.
    """

    reason: str = Field(
        description=(
            "Why this conversation needs a human agent. "
            "Be concise. Customer-facing."
        )
    )
    summary: str | None = Field(
        default=None,
        description=(
            "Optional 1-sentence internal note for the human agent. "
            "Not shown to the customer."
        ),
    )


def make_escalate_tool(
    *,
    conversation_id: str,
    tenant_id: str,
    conv_service: ConversationService | None = None,
) -> BaseTool:
    """Build a configured ``escalate_to_human`` tool bound to one conversation.

    The factory pattern mirrors :func:`agent.graph.nodes.make_llm_node`
    — every per-turn resource is closed over the tool factory so
    the LLM cannot influence tenant scoping.

    Parameters
    ----------
    conversation_id:
        Opaque conversation ULID. Bound into the closure; the
        tool never reads it from the LLM's arguments.
    tenant_id:
        Opaque tenant ULID. Same as above — tenant isolation is
        enforced by closure capture, not by trusting tool args.
    conv_service:
        Conversation service. When ``None`` a fresh
        :class:`ConversationService` is constructed; tests pass
        a spy. The tool mutates conversation state exclusively
        through this surface (no direct DB access).

    Returns
    -------
    langchain_core.tools.BaseTool
        A LangChain tool named ``escalate_to_human`` with
        ``ainvoke`` available for the LLM node to dispatch.

    Notes
    -----
    The returned tool's ``ainvoke`` calls
    :meth:`ConversationService.assign_to_agent` with
    ``agent_id=None``. ``assign_to_agent`` is typed as
    ``agent_id: str`` but semantically treats ``None`` as
    "no agent assigned yet — waiting in queue". We use
    :func:`typing.cast` to document this intentional widening;
    the column itself is nullable, so the assignment is safe
    at runtime.
    """
    if conv_service is None:
        conv_service = ConversationService()

    # Bind these so the inner function closes over them. Renaming
    # to ``_bound_*`` makes accidental reuse from another tool
    # obvious in stack traces.
    _bound_tenant_id = tenant_id
    _bound_conversation_id = conversation_id
    _bound_conv_service = conv_service

    @tool("escalate_to_human", args_schema=EscalateArgs)
    async def escalate_to_human(
        reason: str,
        summary: str | None = None,
    ) -> dict[str, Any]:
        """Escalate the conversation to a human agent.

        Use this tool when the customer is asking for a human,
        the issue cannot be resolved from the knowledge base,
        or you (the assistant) are explicitly uncertain.

        Returns a small dict the caller can surface to the
        customer. Do NOT include any internal IDs in the
        response.
        """
        try:
            await _bound_conv_service.assign_to_agent(
                tenant_id=_bound_tenant_id,
                conversation_id=_bound_conversation_id,
                agent_id=cast(str, None),
            )
        except Exception as exc:
            # The tool MUST NOT crash the LLM turn. A failed
            # escalation just means the conversation stays in
            # AI handling — the caller will fall back to the
            # LLM's normal text response. PII-safe log line.
            log.warning(
                "agent.graph.escalation_failed",
                tenant_id=_bound_tenant_id,
                conversation_id=_bound_conversation_id,
                error_type=type(exc).__name__,
            )
            # Re-raise so the llm_node's tool-ainvoke path
            # records the failure and falls back. The fallback
            # contract lives in nodes.py, not here.
            raise

        log.info(
            "agent.graph.escalated_to_human",
            tenant_id=_bound_tenant_id,
            conversation_id=_bound_conversation_id,
            has_summary=1 if summary else 0,
        )
        # Customer-facing payload. The caller turns this into
        # ``state["escalation_message"]``; we don't include
        # ``summary`` (internal-only).
        return {"escalated": True, "reason": reason}

    # The decorator returns a ``BaseTool`` (StructuredTool); the
    # inner ``async def`` is rebound to that tool object so the
    # factory returns the tool directly.
    return escalate_to_human


# Public surface for tests / future tool additions.
__all__ = [
    "EscalateArgs",
    "make_escalate_tool",
]
