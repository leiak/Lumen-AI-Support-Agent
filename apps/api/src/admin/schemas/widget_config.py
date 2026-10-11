"""Pydantic schemas for tenant widget appearance config.

Tier 2 Task 2.4 — Widget config CRUD.

One row per tenant. The widget SPA reads this on boot to render
its accent color, position, and welcome banner. Defaults match the
``apps/web-sdk`` hardcoded values from M2.A so a freshly-seeded
tenant looks identical to the M2 baseline.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

WidgetTheme = Literal["light", "dark", "auto"]
WidgetPosition = Literal["bottom-right", "bottom-left"]

WIDGET_THEMES: list[WidgetTheme] = ["light", "dark", "auto"]
WIDGET_POSITIONS: list[WidgetPosition] = ["bottom-right", "bottom-left"]

# Brand-friendly defaults — matches the M2.A baseline.
DEFAULT_PRIMARY_COLOR = "#2563eb"
DEFAULT_WIDGET_TITLE = "在线客服"
DEFAULT_WELCOME_MESSAGE = "您好!请描述您的问题,我们会尽快为您解答。"


class WidgetConfigRead(BaseModel):
    """Response body — the current widget appearance config."""

    tenant_id: str
    theme: WidgetTheme
    primary_color: str = Field(
        ...,
        pattern=r"^#[0-9A-Fa-f]{6}$",
        description="Hex color string, e.g. '#2563eb'.",
    )
    position: WidgetPosition
    widget_title: str = Field(..., min_length=1, max_length=64)
    welcome_message: str = Field(..., min_length=1, max_length=500)
    logo_url: str | None = Field(default=None, max_length=500)
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WidgetConfigUpdate(BaseModel):
    """Request body for PATCH /api/v1/admin/tenants/{tid}/widget-config.

    All fields optional. ``None`` / omitted means "leave this column
    alone" — uses the same `_UNSET` discipline as the LLM Configs
    update path (Pack C #1).
    """

    theme: WidgetTheme | None = None
    primary_color: str | None = Field(
        default=None,
        pattern=r"^#[0-9A-Fa-f]{6}$",
    )
    position: WidgetPosition | None = None
    widget_title: str | None = Field(default=None, min_length=1, max_length=64)
    welcome_message: str | None = Field(default=None, min_length=1, max_length=500)
    logo_url: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None


class WidgetConfigResetResponse(BaseModel):
    """Response body for DELETE /api/v1/admin/tenants/{tid}/widget-config."""

    tenant_id: str
    reset: bool = True


__all__ = [
    "DEFAULT_PRIMARY_COLOR",
    "DEFAULT_WELCOME_MESSAGE",
    "DEFAULT_WIDGET_TITLE",
    "WidgetConfigRead",
    "WidgetConfigResetResponse",
    "WidgetConfigUpdate",
]