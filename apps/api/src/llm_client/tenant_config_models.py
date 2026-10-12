"""ORM model + repository for ``tenant_llm_configs`` (M4.C BYOK + M5.2).

Schema per M4.C spec §6.1: per-(tenant, provider) row with Fernet-
encrypted API key, optional base_url override, and an enabled flag
for ops-level disable without row deletion. M5.2 adds an optional
``model`` column for per-tenant LLM model override (NULL = use
project default).
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
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base, get_sessionmaker
from core.id_gen import new_id

# Sentinel for ``update()``: distinguishes "argument omitted" from "argument
# explicitly passed as None". Used by ``base_url`` and ``model`` so callers
# can clear those columns by passing ``None`` (Pydantic-friendly) without us
# treating it as "field not provided". Mark as ``object`` so mypy stops
# complaining about the union type leaking into the public API.
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
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
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
        model: str | None = None,
        session: AsyncSession | None = None,
    ) -> TenantLLMConfig:
        """Insert or update a (tenant_id, provider_name) row.

        Uses INSERT ... ON CONFLICT (Postgres-specific) for atomicity.
        The unique constraint enforces one row per pair.

        ``model`` is the M5.2 per-tenant model override. ``None`` (the
        default) means "no override; project default applies". Existing
        callers that don't pass ``model`` keep their current behavior
        (NULL column = no override).

        ``session`` (M5.3 Task 2) — when provided, the call uses the
        caller's session and does NOT commit (caller owns the
        transaction; the wrapper composes the upsert + audit INSERT
        into one atomic operation). When ``None`` (default,
        back-compat for the seed script + existing tests), the call
        opens its own session and commits at the end.
        """
        stmt = (
            pg_insert(TenantLLMConfig)
            .values(
                id=new_id(),
                tenant_id=tenant_id,
                provider_name=provider_name,
                encrypted_api_key=encrypted_api_key,
                base_url=base_url,
                model=model,
                enabled=enabled,
            )
            .on_conflict_do_update(
                constraint="uq_tenant_llm_configs_tenant_provider",
                set_={
                    "encrypted_api_key": encrypted_api_key,
                    "base_url": base_url,
                    "model": model,
                    "enabled": enabled,
                    "updated_at": func.now(),
                },
            )
            .returning(TenantLLMConfig)
        )
        if session is not None:
            # Caller-managed transaction (e.g. wrapper composing
            # audit INSERT in the same TX).
            result = await session.execute(stmt)
            row_obj = result.scalar_one()
            await session.flush()
            await session.refresh(row_obj)
            return row_obj
        # Back-compat path: own session + commit.
        sm = get_sessionmaker()
        async with sm() as own_session:
            result = await own_session.execute(stmt)
            row_obj = result.scalar_one()
            await own_session.commit()
            await own_session.refresh(row_obj)
            return row_obj

    async def update(
        self,
        *,
        tenant_id: str,
        provider_name: str,
        enabled: bool | None = None,
        base_url: str | object | None = _UNSET,
        model: str | object | None = _UNSET,
        session: AsyncSession | None = None,
    ) -> TenantLLMConfig | None:
        """Update mutable sidecar fields without rotating the API key.

        Tier 1 Task 1.3: enables the admin SPA to flip ``enabled`` or
        change ``base_url`` without re-submitting the plaintext key
        (which would re-encrypt it under Fernet). M5.2 adds ``model``
        (per-tenant model override). Fields can be omitted by passing
        ``None`` (``enabled``) or the sentinel ``_UNSET`` (``base_url``
        / ``model``). Omitting a field is a no-op; passing ``None`` for
        ``base_url`` / ``model`` clears it. Returns ``None`` if no row
        matches.

        ``session`` (M5.3 Task 2) — when provided, the call uses the
        caller's session and does NOT commit (wrapper composes the
        update + audit INSERT in one TX). When ``None`` (default,
        back-compat), the call opens its own session + commits.
        """
        values: dict[str, object] = {"updated_at": func.now()}
        if enabled is not None:
            values["enabled"] = enabled
        if base_url is not _UNSET:
            values["base_url"] = base_url  # may be None to clear
        if model is not _UNSET:
            values["model"] = model  # may be None to clear
        stmt = (
            update(TenantLLMConfig)
            .where(
                TenantLLMConfig.tenant_id == tenant_id,
                TenantLLMConfig.provider_name == provider_name,
            )
            .values(**values)
            .returning(TenantLLMConfig)
        )
        if session is not None:
            result = await session.execute(stmt)
            row_obj = result.scalar_one_or_none()
            await session.flush()
            if row_obj is not None:
                await session.refresh(row_obj)
            return row_obj
        sm = get_sessionmaker()
        async with sm() as own_session:
            result = await own_session.execute(stmt)
            row_obj = result.scalar_one_or_none()
            await own_session.commit()
            if row_obj is not None:
                await own_session.refresh(row_obj)
            return row_obj

    async def delete_by_tenant_provider(
        self,
        *,
        tenant_id: str,
        provider_name: str,
        session: AsyncSession | None = None,
    ) -> bool:
        """Delete the row matching ``(tenant_id, provider_name)``.

        Returns ``True`` if a row was deleted, ``False`` if no row
        matched (idempotent). Used by the admin SPA's "remove BYOK"
        action — the operator can always re-add the provider with
        POST afterwards.

        ``session`` (M5.3 Task 2) — when provided, uses the caller's
        session and does NOT commit (wrapper composes the delete +
        audit INSERT in one TX). When ``None`` (default, back-compat),
        opens own session + commits.
        """
        stmt = delete(TenantLLMConfig).where(
            TenantLLMConfig.tenant_id == tenant_id,
            TenantLLMConfig.provider_name == provider_name,
        )
        if session is not None:
            result = await session.execute(stmt)
            await session.flush()
            return (result.rowcount or 0) > 0
        sm = get_sessionmaker()
        async with sm() as own_session:
            result = await own_session.execute(stmt)
            await own_session.commit()
            return (result.rowcount or 0) > 0


__all__ = ["TenantLLMConfig", "TenantLLMConfigRepository"]
