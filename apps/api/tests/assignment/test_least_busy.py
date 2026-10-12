"""Tests for AssignmentPolicyService.least_busy + enabled_for + select_agent.

Integration tests: the strategy engine talks to the real DB to
load users + count conversations. The pool of candidates and their
loads is fully controllable, so we exercise every branch of
``least_busy`` without a long-running fixture.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from assignment.service import (
    AssignmentPolicyService,
    default_resolver,
    get_default_service,
)
from channel.enums import ChannelType
from channel.repository import ChannelRepository
# Importing the Channel + Ticket ORM models registers their tables with
# ``Base.metadata`` so the Conversation model's string-based FKs
# ("channels.id", "tickets.id") resolve when we insert a Conversation
# in this isolated test. The repo app's lifespan imports all models
# eagerly — tests have to mimic that selectively.
from channel.models import Channel  # noqa: F401
from conversation.enums import ConversationStatus
from conversation.models import Conversation
from core.database import get_session
from core.id_gen import new_id
# Ticket FK target — see comment above.
from ticket.models import Ticket  # noqa: F401
from tenant.enums import TenantPlan, TenantStatus, UserRole
from tenant.models import Tenant, User
from tenant.repository import TenantRepository, UserRepository


async def _mk_tenant(name: str) -> str:
    """Create a throwaway tenant and return its id. Caller is responsible
    for cleanup via ``_cleanup_tenant``."""
    tid = new_id()
    async with get_session() as session:
        t = Tenant(
            id=tid, name=name, plan=TenantPlan.FREE, status=TenantStatus.ACTIVE
        )
        session.add(t)
        await session.flush()
        await session.commit()
    return tid


async def _cleanup_tenant(tid: str) -> None:
    """Hard-delete a tenant (cascade kills users + conversations)."""
    async with get_session() as session:
        t = await session.get(Tenant, tid)
        if t:
            await session.delete(t)
            await session.commit()


async def _mk_user(tenant_id: str, *, role: UserRole, is_active: bool = True) -> str:
    repo = UserRepository()
    u = await repo.create(
        tenant_id=tenant_id,
        email=f"u-{new_id()}@x.com",
        password_hash="h",  # noqa: S106
        role=role,
        is_active=is_active,
    )
    return u.id


async def _mk_user_with_id(tenant_id: str, user_id: str, *, role: UserRole) -> None:
    """Insert a user with a specific id (so the tiebreak test can
    sort alphabetically)."""
    async with get_session() as session:
        session.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                email=f"{user_id}@x.com",
                password_hash="h",  # noqa: S106
                role=role,
                is_active=True,
            )
        )
        await session.flush()
        await session.commit()


async def _mk_channel(tenant_id: str) -> str:
    repo = ChannelRepository()
    ch = await repo.create(
        tenant_id=tenant_id,
        type=ChannelType.WEB,
        name="lb-test-channel",
        credentials_encrypted="",
    )
    return ch.id


async def _mk_conv(
    tenant_id: str,
    *,
    channel_id: str,
    assigned_agent_id: str | None = None,
    status: ConversationStatus = ConversationStatus.OPEN,
) -> str:
    cid = new_id()
    now = datetime.now(UTC)
    async with get_session() as session:
        c = Conversation(
            id=cid,
            tenant_id=tenant_id,
            channel_id=channel_id,
            customer_external_id=f"cust-{cid}",
            status=status,
            ai_handling=False,
            assigned_agent_id=assigned_agent_id,
            opened_at=now,
            last_activity_at=now,
        )
        session.add(c)
        await session.flush()
        await session.commit()
    return cid


@pytest.mark.integration
async def test_least_busy_single_candidate_picks_them() -> None:
    """One eligible agent → they get the conversation, regardless of load."""
    tid = await _mk_tenant("lb-single")
    try:
        await _mk_user(tid, role=UserRole.AGENT, is_active=True)
        svc = AssignmentPolicyService()
        result = await svc.least_busy(tid)
        assert result is not None
    finally:
        await _cleanup_tenant(tid)


@pytest.mark.integration
async def test_least_busy_picks_lowest_load_tiebreak_by_id() -> None:
    """Three agents, one with the fewest open+pending — wins.

    Also covers the tiebreak: when two agents have equal load, the
    lexicographically smaller id wins.
    """
    tid = await _mk_tenant("lb-tiebreak")
    try:
        channel_id = await _mk_channel(tid)
        # Use deliberately sortable ids so the tiebreak is
        # unambiguous. smaller id = should win ties.
        a_id = "01HZX00000000000000000A"  # load=5
        b_id = "01HZX00000000000000000B"  # load=2
        c_id = "01HZX00000000000000000C"  # load=10
        for uid in (a_id, b_id, c_id):
            await _mk_user_with_id(tid, uid, role=UserRole.AGENT)
        # 5 convs for a, 2 for b, 10 for c → b wins.
        for _ in range(5):
            await _mk_conv(tid, channel_id=channel_id, assigned_agent_id=a_id)
        for _ in range(2):
            await _mk_conv(tid, channel_id=channel_id, assigned_agent_id=b_id)
        for _ in range(10):
            await _mk_conv(tid, channel_id=channel_id, assigned_agent_id=c_id)

        svc = AssignmentPolicyService()
        assert await svc.least_busy(tid) == b_id

        # Bump b's load to match a's, then a (lex-smaller) should win.
        for _ in range(3):
            await _mk_conv(tid, channel_id=channel_id, assigned_agent_id=b_id)
        assert await svc.least_busy(tid) == a_id
    finally:
        await _cleanup_tenant(tid)


@pytest.mark.integration
async def test_least_busy_excludes_inactive_and_non_assignable_roles() -> None:
    """Inactive users and admin/viewer roles must NOT be candidates.

    Sets up 1 active agent (the only eligible) and verifies they
    are returned even when admin + viewer + inactive-agent users
    exist in the same tenant.
    """
    tid = await _mk_tenant("lb-exclude")
    try:
        eligible = await _mk_user(tid, role=UserRole.AGENT, is_active=True)
        await _mk_user(tid, role=UserRole.AGENT, is_active=False)  # inactive
        await _mk_user(tid, role=UserRole.ADMIN)  # not assignable
        await _mk_user(tid, role=UserRole.VIEWER)  # not assignable

        svc = AssignmentPolicyService()
        assert await svc.least_busy(tid) == eligible
    finally:
        await _cleanup_tenant(tid)


@pytest.mark.integration
async def test_least_busy_no_eligible_candidates_returns_none() -> None:
    """No active agent/supervisor → no one to assign → None.

    Also verifies ``select_agent`` returns None when the tenant
    has no policy configured.
    """
    tid = await _mk_tenant("lb-none")
    try:
        # A user exists but is the wrong role + not active combo.
        await _mk_user(tid, role=UserRole.ADMIN, is_active=True)
        await _mk_user(tid, role=UserRole.AGENT, is_active=False)
        svc = AssignmentPolicyService()
        assert await svc.least_busy(tid) is None
        # select_agent with no policy → None (no policy reads).
        assert await svc.select_agent(tid) is None
    finally:
        await _cleanup_tenant(tid)


@pytest.mark.integration
async def test_enabled_for_reads_settings_shape() -> None:
    """enabled_for honours the policy sub-dict shape.

    Three scenarios:
      * missing policy → False
      * enabled=True, strategy=least_busy → True
      * enabled=True, strategy=manual → False (manual is the
        explicit opt-out sentinel)
    """
    tid = await _mk_tenant("lb-enabled")
    try:
        repo = TenantRepository()
        svc = AssignmentPolicyService(tenant_repo=repo)

        # No settings → False.
        assert await svc.enabled_for(tid) is False
        assert await svc.select_agent(tid) is None

        # Opt-in to least_busy → True.
        await repo.update_settings(
            tid,
            settings={"assignment_policy": {"enabled": True, "strategy": "least_busy"}},
        )
        assert await svc.enabled_for(tid) is True

        # Switch to manual → False (admin explicitly chose manual).
        await repo.update_settings(
            tid,
            settings={"assignment_policy": {"enabled": True, "strategy": "manual"}},
        )
        assert await svc.enabled_for(tid) is False
        assert await svc.select_agent(tid) is None

        # enabled=False, strategy=least_busy → False.
        await repo.update_settings(
            tid,
            settings={"assignment_policy": {"enabled": False, "strategy": "least_busy"}},
        )
        assert await svc.enabled_for(tid) is False
    finally:
        await _cleanup_tenant(tid)


@pytest.mark.integration
async def test_default_resolver_is_singleton_and_safe_to_call() -> None:
    """default_resolver / get_default_service are idempotent and side-effect-free."""
    s1 = get_default_service()
    s2 = get_default_service()
    assert s1 is s2
    # default_resolver on a missing tenant is a no-op (None).
    assert await default_resolver(new_id()) is None
