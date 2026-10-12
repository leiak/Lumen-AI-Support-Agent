"""Integration tests for TenantRepository and UserRepository. Requires live DB."""
import pytest

from core.database import get_session
from core.id_gen import new_id
from tenant.enums import TenantPlan, TenantStatus, UserRole
from tenant.models import Tenant
from tenant.repository import TenantRepository, UserRepository


@pytest.mark.integration
async def test_tenant_repository_create_and_get() -> None:
    repo = TenantRepository()
    tenant = await repo.create(name="Acme Test", plan=TenantPlan.PRO)
    assert tenant.id
    assert len(tenant.id) == 26  # ULID
    assert tenant.status == TenantStatus.ACTIVE

    try:
        loaded = await repo.get_by_id(tenant.id)
        assert loaded is not None
        assert loaded.name == "Acme Test"
        assert loaded.plan == TenantPlan.PRO
    finally:
        # Idempotent cleanup
        async with get_session() as session:
            t = await session.get(Tenant, tenant.id)
            if t:
                await session.delete(t)
                await session.commit()


@pytest.mark.integration
async def test_user_repository_unique_per_tenant() -> None:
    # Set up tenant
    tenant_id = new_id()
    async with get_session() as session:
        t = Tenant(id=tenant_id, name="X", plan=TenantPlan.FREE)
        session.add(t)
        await session.flush()
        await session.commit()

    repo = UserRepository()
    email = f"a-{tenant_id}@x.com"  # unique per test run
    try:
        u1 = await repo.create(
            tenant_id=tenant_id,
            email=email,
            password_hash="h",  # noqa: S106
            role=UserRole.AGENT,
        )
        assert u1.id
        assert u1.email == email

        # Second insert with same (tenant_id, email) should violate unique constraint
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            await repo.create(
                tenant_id=tenant_id,
                email=email,
                password_hash="h",  # noqa: S106
                role=UserRole.AGENT,
            )
    finally:
        # Cleanup tenant (cascade deletes users)
        async with get_session() as session:
            t = await session.get(Tenant, tenant_id)
            if t:
                await session.delete(t)
                await session.commit()


@pytest.mark.integration
async def test_user_repository_get_by_email() -> None:
    tenant_id = new_id()
    async with get_session() as session:
        t = Tenant(id=tenant_id, name="Y", plan=TenantPlan.FREE)
        session.add(t)
        await session.flush()
        await session.commit()

    repo = UserRepository()
    email = f"b-{tenant_id}@y.com"
    try:
        created = await repo.create(
            tenant_id=tenant_id,
            email=email,
            password_hash="h",  # noqa: S106
            role=UserRole.AGENT,
        )
        loaded = await repo.get_by_email(tenant_id, email)
        assert loaded is not None
        assert loaded.id == created.id
    finally:
        async with get_session() as session:
            t = await session.get(Tenant, tenant_id)
            if t:
                await session.delete(t)
                await session.commit()


@pytest.mark.integration
async def test_tenant_settings_round_trip() -> None:
    """update_settings / get_settings / get_assignment_policy round-trip.

    Backing storage for the per-tenant assignment policy
    (apps/api/src/assignment/service.py). Locks down three guarantees:
      * the whole-dict writer replaces atomically
      * the get_settings reader returns the fresh dict
      * get_assignment_policy returns only the assignment_policy sub-dict
        (or None when missing)
    """
    tenant_id = new_id()
    async with get_session() as session:
        t = Tenant(id=tenant_id, name="settings-test", plan=TenantPlan.FREE)
        session.add(t)
        await session.flush()
        await session.commit()

    repo = TenantRepository()
    try:
        # Empty by default.
        initial = await repo.get_settings(tenant_id)
        assert initial == {}
        assert await repo.get_assignment_policy(tenant_id) is None

        # Write a settings payload with two top-level keys.
        await repo.update_settings(
            tenant_id,
            settings={
                "assignment_policy": {
                    "enabled": True,
                    "strategy": "least_busy",
                },
                "feature_flag": {"foo": "bar"},
            },
        )
        reloaded = await repo.get_settings(tenant_id)
        assert reloaded is not None
        assert reloaded["feature_flag"] == {"foo": "bar"}
        policy = await repo.get_assignment_policy(tenant_id)
        assert policy == {"enabled": True, "strategy": "least_busy"}

        # Whole-dict writer replaces (not merges).
        await repo.update_settings(tenant_id, settings={"other": 1})
        final = await repo.get_settings(tenant_id)
        assert final == {"other": 1}
        # Old keys gone — assignment_policy is now missing.
        assert await repo.get_assignment_policy(tenant_id) is None
    finally:
        async with get_session() as session:
            t = await session.get(Tenant, tenant_id)
            if t:
                await session.delete(t)
                await session.commit()


@pytest.mark.integration
async def test_tenant_settings_returns_none_for_missing_tenant() -> None:
    """get_settings / get_assignment_policy for a deleted tenant must return None.

    Strategy layer relies on this None to fall back to default
    (disabled) behaviour without checking existence separately.
    """
    repo = TenantRepository()
    missing_id = new_id()
    assert await repo.get_settings(missing_id) is None
    assert await repo.get_assignment_policy(missing_id) is None
    # update_settings on a missing tenant returns None (no crash).
    result = await repo.update_settings(missing_id, settings={"x": 1})
    assert result is None
