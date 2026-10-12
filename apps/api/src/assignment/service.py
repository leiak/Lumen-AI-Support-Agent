"""AssignmentPolicyService — pick an agent for an escalated conversation.

Strategy registry
-----------------

The service dispatches on the ``strategy`` field stored in
``tenants.settings['assignment_policy']``. Today only one strategy
is implemented (``least_busy``); the registry makes adding the
next one a one-method change.

Public surface (all async, all tenant-scoped):

* :meth:`AssignmentPolicyService.enabled_for` — short-circuits the
  no-policy case so callers don't pay the candidate-list +
  group-by query when the tenant hasn't opted in.
* :meth:`AssignmentPolicyService.select_agent` — the resolver that
  :class:`conversation.service.ConversationService` calls after
  ``escalate_to_human_queue``. Returns an agent id, or ``None`` to
  fall back to the current behaviour (PENDING + NULL, agent
  claims manually).

The class is intentionally stateless — every method takes
``tenant_id`` as its primary input. Safe to share across requests
as a singleton.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from conversation.repository import ConversationRepository
from tenant.enums import UserRole
from tenant.repository import TenantRepository, UserRepository

logger = logging.getLogger(__name__)


# Type alias for the seam ConversationService.escalate_to_human_queue
# invokes. Returning ``None`` means "no decision" — caller falls back
# to the current PENDING + NULL behaviour.
PolicyResolver = Callable[[str], Awaitable[str | None]]


# Roles that can be auto-assigned a conversation. Admin and viewer
# never appear in the agent dropdown (admin would defeat the
# purpose, viewer can't take ownership).
ASSIGNABLE_ROLES: tuple[UserRole, ...] = (UserRole.AGENT, UserRole.SUPERVISOR)


class AssignmentPolicyService:
    """Stateless per-tenant policy engine."""

    def __init__(
        self,
        *,
        tenant_repo: TenantRepository | None = None,
        user_repo: UserRepository | None = None,
        conversation_repo: ConversationRepository | None = None,
    ) -> None:
        # Repos are injectable so tests can swap in fakes without
        # touching the real DB. Production code uses the defaults,
        # which lazily construct repos at first call.
        self._tenant_repo = tenant_repo
        self._user_repo = user_repo
        self._conversation_repo = conversation_repo

    @property
    def tenant_repo(self) -> TenantRepository:
        if self._tenant_repo is None:
            self._tenant_repo = TenantRepository()
        return self._tenant_repo

    @property
    def user_repo(self) -> UserRepository:
        if self._user_repo is None:
            self._user_repo = UserRepository()
        return self._user_repo

    @property
    def conversation_repo(self) -> ConversationRepository:
        if self._conversation_repo is None:
            self._conversation_repo = ConversationRepository()
        return self._conversation_repo

    async def enabled_for(self, tenant_id: str) -> bool:
        """Return True iff the tenant has an active, recognised policy.

        Missing tenant, missing settings, missing key, disabled
        flag, or unknown strategy all return False — caller's
        default behaviour is "no auto-assign, let agents claim".
        """
        policy = await self.tenant_repo.get_assignment_policy(tenant_id)
        if not policy:
            return False
        if not policy.get("enabled"):
            return False
        strategy = policy.get("strategy")
        return strategy in {"least_busy", "manual"} and strategy != "manual"

    async def select_agent(self, tenant_id: str) -> str | None:
        """Pick an agent id for the escalated conversation.

        Dispatched by ``strategy``. Returns ``None`` when the
        tenant has no policy, has no eligible candidates, or the
        configured strategy is unknown — caller treats None as
        "leave the conversation unassigned".

        The escalation path is best-effort: any unexpected
        exception is logged at WARNING and swallowed so the
        customer-facing inbound flow isn't poisoned by a
        policy-engine blip. The conversation still lands in the
        human queue (status=PENDING, agent=NULL).
        """
        policy = await self.tenant_repo.get_assignment_policy(tenant_id)
        if not policy or not policy.get("enabled"):
            return None
        strategy = policy.get("strategy")
        if strategy == "manual":
            return None
        if strategy != "least_busy":
            logger.warning(
                "assignment_policy.unknown_strategy",
                extra={"tenant_id": tenant_id, "strategy": strategy},
            )
            return None
        try:
            return await self.least_busy(tenant_id)
        except Exception:  # noqa: BLE001 — best-effort
            logger.warning(
                "assignment_policy.select_agent_failed",
                extra={"tenant_id": tenant_id, "strategy": strategy},
                exc_info=True,
            )
            return None

    async def least_busy(self, tenant_id: str) -> str | None:
        """Pick the active agent+supervisor with the fewest open+pending conversations.

        Tiebreak: when multiple agents share the minimum load, the
        one with the lexicographically smallest ``id`` wins. This
        keeps the selection deterministic for the same input, so
        QA eval / tests can pin it without flakiness.

        Returns ``None`` when no eligible candidates exist (no
        active agent+supervisor users for the tenant, or the
        tenant doesn't exist). Inactive users (``is_active=False``)
        are excluded so a deactivated agent doesn't get
        auto-assigned.
        """
        users = await self.user_repo.list_for_tenant(tenant_id)
        eligible_ids = sorted(
            u.id
            for u in users
            if u.is_active and u.role in ASSIGNABLE_ROLES
        )
        if not eligible_ids:
            return None
        loads = await self.conversation_repo.count_open_pending_per_agent(
            tenant_id=tenant_id,
            candidate_ids=eligible_ids,
        )
        # loads is {agent_id: int}; min with deterministic tiebreak.
        return min(eligible_ids, key=lambda aid: (loads.get(aid, 0), aid))


# Module-level singleton — the escalation path calls this
# indirectly via ConversationService's default resolver. Stateless
# and safe to share.
_default_service: AssignmentPolicyService | None = None


def get_default_service() -> AssignmentPolicyService:
    """Lazily build a process-wide AssignmentPolicyService."""
    global _default_service
    if _default_service is None:
        _default_service = AssignmentPolicyService()
    return _default_service


async def default_resolver(tenant_id: str) -> str | None:
    """Adapter used by ConversationService when no resolver is injected.

    Kept as a free function (not a method) so tests can pass it
    directly into the ConversationService ctor without instantiating
    a real AssignmentPolicyService.
    """
    return await get_default_service().select_agent(tenant_id)
