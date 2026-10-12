"""ORM model + repository for ``tenant_fallback_chains`` (M5.1).

Per-tenant LLM fallback chain — an ordered list of ``{provider, model,
enabled}`` dicts the gateway walks through in sequence when a tenant's
primary LLM call fails (rate limit, transient provider outage, etc.).
One row per tenant; ``tenant_id`` carries the unique index.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base, get_sessionmaker
from core.id_gen import new_id

# Mirrors the project's Literal["minimax", "anthropic", "openai"]
# provider allowlist (see llm_client.provider_registry).
_ALLOWED_PROVIDERS: frozenset[str] = frozenset({"minimax", "anthropic", "openai"})
_MODEL_MAX_LEN: int = 128


def _validate_steps(steps: list[dict[str, Any]]) -> None:
    """Reject malformed step dicts or all-disabled chains.

    Rules (per M5.1 spec):
    - Each step must have ``provider`` in the allowed set
      (``ValueError(f"unknown provider {v!r}")``).
    - ``model`` must be a string of length 1-128.
    - ``enabled`` must be a bool (defaults to True when omitted).
    - At least one step must be enabled
      (``ValueError("chain must contain at least one enabled step")``).
    - Empty steps list is rejected with the same message.
    """
    if not steps:
        raise ValueError("chain must contain at least one enabled step")
    has_enabled = False
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError(
                f"step must be a dict, got {type(step).__name__}"
            )
        provider = step.get("provider")
        if provider not in _ALLOWED_PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        model = step.get("model")
        if not isinstance(model, str) or not (1 <= len(model) <= _MODEL_MAX_LEN):
            raise ValueError(
                f"model must be a string of length 1-{_MODEL_MAX_LEN}; "
                f"got {model!r}"
            )
        enabled = step.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"enabled must be a bool; got {enabled!r}")
        if enabled:
            has_enabled = True
    if not has_enabled:
        raise ValueError("chain must contain at least one enabled step")


class TenantFallbackChain(Base):
    """One row per tenant — the ordered fallback chain for that tenant."""

    __tablename__ = "tenant_fallback_chains"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_fallback_chains_tenant_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list,
    )
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


class TenantFallbackChainRepository:
    """CRUD for ``TenantFallbackChain`` rows."""

    async def get(self, tenant_id: str) -> TenantFallbackChain | None:
        """Return the chain for ``tenant_id``, or ``None`` if missing."""
        sm = get_sessionmaker()
        async with sm() as session:
            stmt = select(TenantFallbackChain).where(
                TenantFallbackChain.tenant_id == tenant_id,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def upsert(
        self,
        *,
        tenant_id: str,
        steps: list[dict[str, Any]],
        enabled: bool = True,
    ) -> TenantFallbackChain:
        """Insert or update the chain for ``tenant_id``.

        Validates the ``steps`` list before touching the DB. Uses
        ``INSERT ... ON CONFLICT`` (Postgres-specific) keyed on the
        ``uq_tenant_fallback_chains_tenant_id`` unique constraint for
        atomicity — a concurrent admin SPA save can never produce two
        rows for the same tenant.
        """
        _validate_steps(steps)
        sm = get_sessionmaker()
        async with sm() as session:
            stmt = (
                pg_insert(TenantFallbackChain)
                .values(
                    id=new_id(),
                    tenant_id=tenant_id,
                    steps=steps,
                    enabled=enabled,
                )
                .on_conflict_do_update(
                    constraint="uq_tenant_fallback_chains_tenant_id",
                    set_={
                        "steps": steps,
                        "enabled": enabled,
                        "updated_at": func.now(),
                    },
                )
                .returning(TenantFallbackChain)
            )
            result = await session.execute(stmt)
            row_obj = result.scalar_one()
            await session.commit()
            await session.refresh(row_obj)
            return row_obj

    async def delete(self, tenant_id: str) -> bool:
        """Delete the row for ``tenant_id``.

        Returns ``True`` if a row was deleted, ``False`` if no row
        matched (idempotent — callers can safely retry).
        """
        sm = get_sessionmaker()
        async with sm() as session:
            stmt = delete(TenantFallbackChain).where(
                TenantFallbackChain.tenant_id == tenant_id,
            )
            result = await session.execute(stmt)
            await session.commit()
            return (result.rowcount or 0) > 0


__all__ = ["TenantFallbackChain", "TenantFallbackChainRepository"]