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
    delete,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base, get_sessionmaker
from core.id_gen import new_id

# Sentinel for ``update()``: distinguishes "argument omitted" from "argument
# explicitly passed as None". Used by ``base_url`` so callers can clear the
# column by passing ``None`` (Pydantic-friendly) without us treating it as
# "field not provided". Mark as ``object`` so mypy stops complaining about
# the union type leaking into the public API.
_UNSET: object = object()


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

    async def update(
        self,
        *,
        tenant_id: str,
        provider_name: str,
        enabled: bool | None = None,
        base_url: str | None | object = _UNSET,
    ) -> TenantLLMConfig | None:
        """Update mutable sidecar fields without rotating the API key.

        Tier 1 Task 1.3: enables the admin SPA to flip ``enabled`` or
        change ``base_url`` without re-submitting the plaintext key
        (which would re-encrypt it under Fernet). Either field can be
        omitted by passing ``None`` (``enabled``) or the sentinel
        ``_UNSET`` (``base_url``). Returns ``None`` if no row matches.
        """
        sm = get_sessionmaker()
        async with sm() as session:
            values: dict[str, object] = {"updated_at": func.now()}
            if enabled is not None:
                values["enabled"] = enabled
            if base_url is not _UNSET:
                values["base_url"] = base_url  # may be None to clear
            stmt = (
                update(TenantLLMConfig)
                .where(
                    TenantLLMConfig.tenant_id == tenant_id,
                    TenantLLMConfig.provider_name == provider_name,
                )
                .values(**values)
                .returning(TenantLLMConfig)
            )
            result = await session.execute(stmt)
            row_obj = result.scalar_one_or_none()
            await session.commit()
            if row_obj is not None:
                await session.refresh(row_obj)
            return row_obj

    async def delete_by_tenant_provider(
        self, *, tenant_id: str, provider_name: str,
    ) -> bool:
        """Delete the row matching ``(tenant_id, provider_name)``.

        Returns ``True`` if a row was deleted, ``False`` if no row
        matched (idempotent). Used by the admin SPA's "remove BYOK"
        action — the operator can always re-add the provider with
        POST afterwards.
        """
        sm = get_sessionmaker()
        async with sm() as session:
            stmt = delete(TenantLLMConfig).where(
                TenantLLMConfig.tenant_id == tenant_id,
                TenantLLMConfig.provider_name == provider_name,
            )
            result = await session.execute(stmt)
            await session.commit()
            return (result.rowcount or 0) > 0


__all__ = ["TenantLLMConfig", "TenantLLMConfigRepository"]
