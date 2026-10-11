"""Pydantic schemas for tenant self-settings admin endpoints.

Tier 2 Task 2.3 — Tenant management.

Scope: a tenant-scoped admin can view + edit their own tenant's
``name`` / ``plan`` / ``status``. Listing all tenants (cross-tenant)
would require a super-admin role that does NOT exist in the current
JWT model (``require_admin`` derives every check from
``claims['tenant_id']``). That's deliberately out of scope for this
task — adding a super-admin would require a separate auth path and
new role enum, and the SPA already lets the operator navigate to a
single tenant's settings page via the URL.

PII discipline
--------------

``name`` is the tenant's chosen display name (PII-light). The wire
shape returns it directly; we never log it.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from tenant.enums import TenantPlan, TenantStatus


class TenantSettingsRead(BaseModel):
    """Response body — the current tenant's settings.

    Powers ``GET /api/v1/admin/tenants/{tenant_id}/settings``. Returns
    the immutable ``id`` + the editable fields. ``settings`` (the
    JSONB bag) is intentionally omitted — M1 doesn't have any
    schema'd use for it yet; we'll surface it when we do.
    """

    id: str
    name: str
    plan: TenantPlan
    status: TenantStatus
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TenantSettingsUpdate(BaseModel):
    """Request body for ``PATCH /api/v1/admin/tenants/{tenant_id}/settings``.

    All fields optional — omitted fields are preserved. The
    server enforces the ``require_admin`` gate; ``status`` changes
    are restricted to the values we want tenants to be able to set
    (no direct DELETED transition; that's reserved for super-admin
    in a future task).
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    plan: TenantPlan | None = None
    status: TenantStatus | None = None


__all__ = ["TenantSettingsRead", "TenantSettingsUpdate"]