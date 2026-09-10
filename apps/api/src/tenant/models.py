"""Tenant and User ORM models. ULID primary keys, FK with cascade, unique
constraint on (tenant_id, email). JSON settings column on Tenant for flexible
per-tenant config."""
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base
from tenant.enums import TenantPlan, TenantStatus, UserRole


class Tenant(Base):
    """A workspace (customer organization) in the SaaS."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    plan: Mapped[TenantPlan] = mapped_column(
        String(20), nullable=False, default=TenantPlan.FREE
    )
    status: Mapped[TenantStatus] = mapped_column(
        String(20), nullable=False, default=TenantStatus.ACTIVE
    )
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    users: Mapped[list["User"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class User(Base):
    """A human user (agent, supervisor, admin) belonging to one Tenant."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_user_tenant_email"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[UserRole] = mapped_column(
        String(20), nullable=False, default=UserRole.AGENT
    )
    sso_subject: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="users")