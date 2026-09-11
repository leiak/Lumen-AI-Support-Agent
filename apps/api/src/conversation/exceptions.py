"""Domain-level exceptions raised by ``ConversationService``.

Kept in a dedicated module so the API layer (and any future
internal callers) can catch domain errors without depending on the
service implementation.

Anti-enumeration rule: every 4xx error message raised here is
deliberately generic so a probing caller cannot distinguish between
"conversation doesn't exist", "wrong tenant", "already claimed", or
"wrong status" via the response body. The status code is the only
discriminator the caller sees.
"""
from __future__ import annotations


class ConversationNotClaimableError(Exception):
    """Raised when an agent's claim attempt is rejected.

    Covers two distinct failure modes with a single error class so
    the API layer can map them to ``409 Conflict`` uniformly:

    * The conversation's ``status`` is not ``PENDING`` (e.g. CLOSED,
      or still OPEN because no escalation transition ran).
    * The conversation has already been claimed by another agent
      (``assigned_agent_id IS NOT NULL``).

    Anti-enumeration: the default message wording is identical for
    both branches. Callers must not pass distinguishing context into
    the exception message — the only safe payload is the opaque
    ``conversation_id`` (already visible to the caller as a URL
    parameter) and a generic reason. NEVER include the agent_id of
    the prior claimer, the prior status, or anything else that would
    let a probe caller enumerate state across the tenant boundary.
    """

    def __init__(
        self, message: str = "conversation cannot be claimed"
    ) -> None:
        super().__init__(message)


__all__ = ["ConversationNotClaimableError"]