"""ORM model + repository for ``tenant_llm_configs`` (M4.C BYOK).

Schema per M4.C spec §6.1: per-(tenant, provider) row with Fernet-
encrypted API key, optional base_url override, and an enabled flag
for ops-level disable without row deletion.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base, get_sessionmaker
from core.id_gen import new_id


class TenantLLMConfig(Base):
    """One row = one provider's API key for one tenant."""

    __tablename__ = "tenant_llm_configs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider_name",
            name="uq_tenant_llm_configs_tenant_provider",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_api_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(512))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantLLMConfigRepository:
    """CRUD for ``TenantLLMConfig`` rows."""

    async def list_by_tenant(
        self, tenant_id: str, *, enabled_only: bool = True
    ) -> list[TenantLLMConfig]:
        """Return all configs for a tenant. ``enabled_only`` filters out disabled rows."""
        sm = get_sessionmaker()
        async with sm() as session:
            stmt = select(TenantLLMConfig).where(TenantLLMConfig.tenant_id == tenant_id)
            if enabled_only:
                stmt = stmt.where(TenantLLMConfig.enabled.is_(True))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def upsert(
        self,
        *,
        tenant_id: str,
        provider_name: str,
        encrypted_api_key: bytes,
        base_url: str | None,
        enabled: bool = True,
    ) -> TenantLLMConfig:
        """Insert or update a (tenant_id, provider_name) row.

        Uses INSERT ... ON CONFLICT (Postgres-specific) for atomicity.
        The unique constraint enforces one row per pair.
        """
        sm = get_sessionmaker()
        async with sm() as session:
            stmt = (
                pg_insert(TenantLLMConfig)
                .values(
                    id=new_id(),
                    tenant_id=tenant_id,
                    provider_name=provider_name,
                    encrypted_api_key=encrypted_api_key,
                    base_url=base_url,
                    enabled=enabled,
                )
                .on_conflict_do_update(
                    constraint="uq_tenant_llm_configs_tenant_provider",
                    set_={
                        "encrypted_api_key": encrypted_api_key,
                        "base_url": base_url,
                        "enabled": enabled,
                        "updated_at": func.now(),
                    },
                )
                .returning(TenantLLMConfig)
            )
            result = await session.execute(stmt)
            row_obj = result.scalar_one()
            await session.commit()
            await session.refresh(row_obj)
            return row_obj


__all__ = ["TenantLLMConfig", "TenantLLMConfigRepository"]
