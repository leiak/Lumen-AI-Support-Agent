"""Tests for Tenant and User ORM models. Integration tests require a live DB."""
import pytest
from sqlalchemy import select

from tenant.enums import TenantPlan, TenantStatus, UserRole
from tenant.models import Tenant, User


@pytest.mark.integration
async def test_create_tenant_and_user_roundtrip() -> None:
    from core.database import get_session
    from core.id_gen import new_id

    async with get_session() as session:
        tenant = Tenant(
            id=new_id(),
            name="Acme Corp",
            plan=TenantPlan.PRO,
            status=TenantStatus.ACTIVE,
        )
        session.add(tenant)
        await session.flush()

        user = User(
            id=new_id(),
            tenant_id=tenant.id,
            email="admin@acme.com",
            password_hash="fake-hash",  # noqa: S106
            full_name="Admin User",
            role=UserRole.ADMIN,
        )
        session.add(user)
        await session.flush()
        await session.commit()
        tenant_id = tenant.id

    try:
        # Re-query from a fresh session to verify round-trip
        async with get_session() as session2:
            result = await session2.execute(
                select(User).where(
                    User.tenant_id == tenant_id, User.email == "admin@acme.com"
                )
            )
            loaded = result.scalar_one()
            assert loaded.tenant_id == tenant_id
            assert loaded.role == UserRole.ADMIN
            assert loaded.full_name == "Admin User"
    finally:
        # Clean up so re-running the test is idempotent
        async with get_session() as session3:
            t = await session3.get(Tenant, tenant_id)
            if t is not None:
                await session3.delete(t)
                await session3.commit()


def test_tenant_table_metadata() -> None:
    """Pure-Python test: verify model table names and FK constraints without a DB."""
    from tenant.models import Tenant, User

    assert Tenant.__tablename__ == "tenants"
    assert User.__tablename__ == "users"
    # Verify the unique constraint is declared
    constraints = {c.name for c in User.__table__.constraints}
    assert "uq_user_tenant_email" in constraints
    # Verify FK column exists
    fk_columns = {c.name for c in User.__table__.columns if c.foreign_keys}
    assert "tenant_id" in fk_columns