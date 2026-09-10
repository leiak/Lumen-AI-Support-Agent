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
