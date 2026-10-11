"""ORM models for widget configuration.

Tier 2 Task 2.4 — Per-tenant widget appearance settings. The
``apps/web-sdk`` reads this on boot to render the accent color,
position, and welcome banner. Defaults are server-side so a
freshly-seeded tenant matches the M2.A baseline.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class WidgetConfig(Base):
    """Per-tenant widget appearance config.

    One row per tenant (tenant_id is the PK). Mirrors the
    ``apps/web-sdk`` boot contract (apps/web-sdk/src/index.ts). The
    widget SPA reads these on init and re-renders the bubble.

    PII discipline
    --------------

    ``welcome_message`` is operator-authored copy, not customer
    text — free to display and log.
    """

    __tablename__ = "widget_configs"
    __table_args__ = (
        CheckConstraint(
            "theme IN ('light', 'dark', 'auto')",
            name="ck_widget_configs_theme",
        ),
        CheckConstraint(
            "position IN ('bottom-right', 'bottom-left')",
            name="ck_widget_configs_position",
        ),
        CheckConstraint(
            "primary_color ~ '^#[0-9A-Fa-f]{6}$'",
            name="ck_widget_configs_primary_color_hex",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    theme: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="light"
    )
    primary_color: Mapped[str] = mapped_column(
        String(7), nullable=False, server_default="#2563eb"
    )
    position: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="bottom-right"
    )
    widget_title: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="在线客服"
    )
    welcome_message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="您好!请描述您的问题,我们会尽快为您解答。",
    )
    logo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


__all__ = ["WidgetConfig"]