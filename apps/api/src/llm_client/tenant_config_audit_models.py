"""ORM + repository for ``tenant_llm_config_audit_log`` (M5.3).

Append-only audit log of every field change made via the admin
REST surface to ``tenant_llm_configs``. One row per field change
(``field`` is one of ``api_key`` | ``base_url`` | ``model`` |
``enabled`` | ``__deleted__``).

Mirrors the immutable-row pattern from
:mod:`budget.models.TenantBudgetCredit` (M4.D Pack B). The app
never UPDATEs or DELETEs audit rows; retention is forever (manual
SQL cleanup if needed).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base, get_sessionmaker

# Field value space — Pydantic Literal enforces this at ORM __init__
# time so a typo never reaches the DB. DB column is just VARCHAR(32).
FieldName = Literal[
    "api_key",
    "base_url",
    "model",
    "enabled",
    "__deleted__",
]


class TenantLLMConfigAuditLog(Base):
    """One row per field change to a tenant's LLM config.

    INSERT-only. The app has no UPDATE/DELETE on this table.

    Foreign keys:
    * ``tenant_id`` -> ``tenants.id`` ON DELETE CASCADE — when a
      tenant is dropped, its audit history goes with it.

    No FK to ``tenant_llm_configs`` by design: deleting a source
    row must NOT cascade the audit (the audit is the historical
    record of that deletion).
    """

    __tablename__ = "tenant_llm_config_audit_log"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    field: Mapped[str] = mapped_column(String(32), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __init__(self, **kwargs: object) -> None:
        # Enforce Literal validation on ``field`` so a typo at the
        # ORM boundary never writes a bogus value to the DB column.
        field = kwargs.get("field")
        if field is not None:
            try:
                # Pydantic v2 Literal validation
                from pydantic import TypeAdapter

                TypeAdapter(FieldName).validate_python(field)
            except ValidationError as exc:
                raise ValueError(
                    f"invalid field value {field!r}; "
                    f"must be one of {list(FieldName.__args__)}"
                ) from exc
        super().__init__(**kwargs)


class TenantLLMConfigAuditRepository:
    """CRUD for ``TenantLLMConfigAuditLog`` rows.

    Mutating method (``insert_many``) takes an explicit ``AsyncSession``
    so the wrapper can compose audit writes into the same
    transaction as the mutation (atomicity guarantee — see spec §4
    and §8). Read methods open their own short-lived session — they
    never participate in a write transaction.
    """

    async def insert_many(
        self,
        session: AsyncSession,
        rows: list[TenantLLMConfigAuditLog],
    ) -> None:
        """Batch insert audit rows. Caller owns the transaction (no commit)."""
        if not rows:
            return
        session.add_all(rows)
        await session.flush()

    async def list_by_tenant(
        self,
        tenant_id: str,
        *,
        provider_name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TenantLLMConfigAuditLog]:
        """List audit rows newest-first. Optional provider_name filter.

        Opens its own short-lived session (read-only query, no
        commit needed). Returns the page slice ordered by
        ``changed_at DESC``.
        """
        sm = get_sessionmaker()
        async with sm() as session:
            stmt = select(TenantLLMConfigAuditLog).where(
                TenantLLMConfigAuditLog.tenant_id == tenant_id,
            )
            if provider_name is not None:
                stmt = stmt.where(
                    TenantLLMConfigAuditLog.provider_name == provider_name,
                )
            stmt = (
                stmt.order_by(TenantLLMConfigAuditLog.changed_at.desc())
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def count_by_tenant(
        self,
        tenant_id: str,
        *,
        provider_name: str | None = None,
    ) -> int:
        """Total rows for the same WHERE clause as ``list_by_tenant``.

        Opens its own short-lived session. Used to populate the
        ``{entries, total}`` envelope on the GET endpoint.
        """
        from sqlalchemy import func as sa_func

        sm = get_sessionmaker()
        async with sm() as session:
            stmt = select(sa_func.count(TenantLLMConfigAuditLog.id)).where(
                TenantLLMConfigAuditLog.tenant_id == tenant_id,
            )
            if provider_name is not None:
                stmt = stmt.where(
                    TenantLLMConfigAuditLog.provider_name == provider_name,
                )
            result = await session.execute(stmt)
            return int(result.scalar() or 0)


__all__ = [
    "FieldName",
    "TenantLLMConfigAuditLog",
    "TenantLLMConfigAuditRepository",
]