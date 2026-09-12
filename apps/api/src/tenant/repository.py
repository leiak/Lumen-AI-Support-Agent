"""Repository layer for Tenant and User aggregates.

Repositories wrap SQLAlchemy sessions so callers don't deal with ORM
details. Each method opens its own short-lived session via get_session().
For longer transactions, callers should manage the session themselves.
"""
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.database import get_session
from core.id_gen import new_id
from tenant.enums import TenantPlan, TenantStatus, UserRole
from tenant.models import Tenant, User


class TenantRepository:
    """CRUD for Tenant rows."""

    async def create(
        self,
        *,
        name: str,
        plan: TenantPlan = TenantPlan.FREE,
        status: TenantStatus = TenantStatus.ACTIVE,
        settings: dict[str, Any] | None = None,
    ) -> Tenant:
        """Insert a new Tenant. Returns the persisted Tenant with id populated."""
        async with get_session() as session:
            tenant = Tenant(
                id=new_id(),
                name=name,
                plan=plan,
                status=status,
                settings=settings if settings is not None else {},
            )
            session.add(tenant)
            await session.flush()
            await session.refresh(tenant)
            await session.commit()
            return tenant

    async def get_by_id(self, tenant_id: str) -> Tenant | None:
        """Look up a Tenant by primary key. Returns None if not found."""
        async with get_session() as session:
            return await session.get(Tenant, tenant_id)

    async def get_by_name(self, name: str) -> Tenant | None:
        """Look up a Tenant by exact name. Returns None if not found."""
        async with get_session() as session:
            result = await session.execute(select(Tenant).where(Tenant.name == name))
            return result.scalar_one_or_none()


class UserRepository:
    """CRUD for User rows."""

    async def create(
        self,
        *,
        tenant_id: str,
        email: str,
        password_hash: str,
        role: UserRole = UserRole.AGENT,
        full_name: str | None = None,
        is_active: bool = True,
    ) -> User:
        """Insert a new User. Raises IntegrityError on duplicate (tenant_id, email)."""
        async with get_session() as session:
            user = User(
                id=new_id(),
                tenant_id=tenant_id,
                email=email,
                password_hash=password_hash,
                role=role,
                full_name=full_name,
                is_active=is_active,
            )
            session.add(user)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(user)
            await session.commit()
            return user

    async def get_by_id(self, user_id: str) -> User | None:
        async with get_session() as session:
            return await session.get(User, user_id)

    async def get_by_email(self, tenant_id: str, email: str) -> User | None:
        """Look up a User by (tenant_id, email). Returns None if not found."""
        async with get_session() as session:
            result = await session.execute(
                select(User).where(User.tenant_id == tenant_id, User.email == email)
            )
            return result.scalar_one_or_none()

    async def get_first_by_email(self, email: str) -> User | None:
        """Look up a User by email across ALL tenants.

        Returns the first match ordered by ``id`` ascending for
        determinism — same input always maps to the same row, so the
        tenant-hint endpoint behaves predictably when a single email
        happens to exist in more than one tenant.

        Returns ``None`` if no user with that email exists in any tenant.
        """
        async with get_session() as session:
            result = await session.execute(
                select(User).where(User.email == email).order_by(User.id.asc()).limit(1)
            )
            return result.scalar_one_or_none()

    async def list_for_tenant(self, tenant_id: str) -> list[User]:
        """Return all users for a tenant (no pagination in M1)."""
        async with get_session() as session:
            result = await session.execute(
                select(User).where(User.tenant_id == tenant_id)
            )
            return list(result.scalars().all())
