"""Agent-workspace domain exceptions.

Stage 8.3 introduces :class:`SuggestionServiceNotFoundError` for
the AI-suggested reply endpoint. Mirrors the anti-enumeration
contract used by :class:`conversation.exceptions.ConversationNotClaimableError`:
domain errors carry only the information the caller already has
(opaque ULIDs) and a generic reason. Never include tenant
identifiers or status details that would let a probe caller
enumerate state across the tenant boundary.
"""
from __future__ import annotations


class SuggestionServiceNotFoundError(Exception):
    """Raised when the suggest-reply target conversation is missing.

    Covers two indistinguishable cases for the caller (the API
    layer maps both to ``404 Not Found``):

    * The ``conversation_id`` does not exist.
    * The ``conversation_id`` exists but belongs to a different
      tenant (cross-tenant probe).

    Anti-enumeration parity with the rest of the workspace API:
    the message is deliberately generic so a probing caller
    cannot distinguish "unknown" from "cross-tenant" via the
    response body. The only safe payload is the opaque
    ``conversation_id`` (already visible to the caller as a URL
    parameter) and a generic reason — NEVER include the
    conversation's tenant_id, status, or any other state that
    would leak cross-tenant information.
    """

    def __init__(self, message: str = "conversation not found") -> None:
        super().__init__(message)


__all__ = ["SuggestionServiceNotFoundError"]
