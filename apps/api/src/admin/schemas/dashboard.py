"""Pydantic schemas for the admin dashboard aggregate.

Tier 2 Task 2.5 — Admin Dashboard.

Single endpoint, one payload. ``GET /api/v1/admin/tenants/{tid}/dashboard``
returns aggregate counts the SPA renders as KPI tiles + small charts.

Field notes
-----------

* ``today_utc_start`` — the UTC midnight that anchored ``tickets_today``.
  The SPA renders this as a small "(since {ts})" caption.
* ``period`` + ``period_starts_at`` mirror the budget snapshot fields
  (apps/api/src/budget/resolver.py::_current_period) so the operator
  sees the same window the cap applies to.
* ``tickets_by_status`` / ``tickets_by_priority`` use the same enum
  values that the ticket list endpoint already surfaces — the SPA
  reuses the existing badge styles.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class StatusCount(BaseModel):
    """One bucket in a status / priority breakdown."""

    key: str
    count: int


class DashboardRead(BaseModel):
    """Aggregate metrics for the current tenant.

    Powers ``GET /api/v1/admin/tenants/{tid}/dashboard``. Cheap to
    compute (4-5 SELECT COUNT queries on indexed columns). No PII
    in this payload — only opaque counts.
    """

    # Tickets — created today (UTC) + by status + by priority.
    today_utc_start: datetime
    tickets_today: int
    tickets_by_status: list[StatusCount]
    tickets_by_priority: list[StatusCount]

    # Conversations — currently open (status=open).
    conversations_open: int

    # Budget snapshot — current period.
    period: str  # YYYY-MM
    period_starts_at: datetime
    llm_tokens_this_period: int

    # Users — currently active.
    active_users: int
    total_users: int


__all__ = ["DashboardRead", "StatusCount"]