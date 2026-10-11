"""Repository for the WidgetConfig aggregate.

Tier 2 Task 2.4. One row per tenant. The repository centralizes the
upsert + reset pattern so the route handler stays focused on auth +
serialization.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from core.database import get_session
from widget.models import WidgetConfig


class WidgetConfigRepository:
    """CRUD for per-tenant widget appearance config."""

    async def get(self, tenant_id: str) -> WidgetConfig | None:
        async with get_session() as session:
            return await session.get(WidgetConfig, tenant_id)

    async def list_for_tenants(
        self, tenant_ids: list[str]
    ) -> list[WidgetConfig]:
        """Bulk lookup — useful for the embed-bootstrap path."""
        if not tenant_ids:
            return []
        async with get_session() as session:
            result = await session.execute(
                select(WidgetConfig).where(WidgetConfig.tenant_id.in_(tenant_ids))
            )
            return list(result.scalars().all())

    async def upsert(
        self,
        tenant_id: str,
        *,
        theme: str | None = None,
        primary_color: str | None = None,
        position: str | None = None,
        widget_title: str | None = None,
        welcome_message: str | None = None,
        logo_url: str | None = None,
        enabled: bool | None = None,
    ) -> WidgetConfig:
        """Insert-or-update. Creates the row with defaults if absent.

        Tier 2 Task 2.4 — admin PATCH path. Uses the same ``_UNSET``
        discipline as the LLM Configs update path: ``None`` means
        "leave this column alone" for all fields except
        ``logo_url`` (where ``None`` is a legitimate "remove the
        logo" value).
        """
        async with get_session() as session:
            row = await session.get(WidgetConfig, tenant_id)
            if row is None:
                row = WidgetConfig(tenant_id=tenant_id)
                session.add(row)
            if theme is not None:
                row.theme = theme
            if primary_color is not None:
                row.primary_color = primary_color
            if position is not None:
                row.position = position
            if widget_title is not None:
                row.widget_title = widget_title
            if welcome_message is not None:
                row.welcome_message = welcome_message
            if logo_url is not None:
                # Empty string → treat as "clear the logo"
                row.logo_url = logo_url or None
            if enabled is not None:
                row.enabled = enabled
            await session.commit()
            await session.refresh(row)
            return row

    async def delete(self, tenant_id: str) -> bool:
        """Hard-delete the config row. Returns True if a row was removed.

        Idempotent — a second call returns ``False``. The next GET
        sees the row as absent and the route handler materializes a
        fresh default row in memory.
        """
        async with get_session() as session:
            row = await session.get(WidgetConfig, tenant_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True


__all__ = ["WidgetConfigRepository"]