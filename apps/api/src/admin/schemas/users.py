"""Pydantic schemas for tenant user (agent / admin) admin endpoints.

Tier 2 Task 2.2 — Agent/User management. Read-only ``UserInfo`` already
exists in :mod:`auth.schemas` for the login response; this module adds
the dedicated admin CRUD shapes (``password_hash`` is never exposed).

PII discipline
--------------

``email`` + ``full_name`` are PII. The wire shape returns them to the
caller's tenant only — cross-tenant access returns 404 (enforced by
the route, anti-enumeration). Logs MUST redact these fields; see
:mod:`core.logging`.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from tenant.enums import UserRole


class UserRead(BaseModel):
    """Public representation of a tenant user (no ``password_hash``).

    Powers ``GET /api/v1/admin/tenants/{tenant_id}/users``. Returns all
    user fields except ``password_hash``. ``is_active`` is included so
    the SPA can render disabled accounts distinctly.
    """

    id: str
    tenant_id: str
    email: str
    full_name: str | None
    role: UserRole
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UserCreate(BaseModel):
    """Request body for ``POST /api/v1/admin/tenants/{tenant_id}/users``.

    Tier 2 Task 2.2 — lets the admin SPA provision new agents/admins
    from the UI instead of relying solely on the seed script. Email
    must be unique per tenant (the natural key); password is
    bcrypt-hashed by the route handler before insert.
    """

    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=200)
    role: UserRole = UserRole.AGENT


class UserUpdate(BaseModel):
    """Request body for ``PATCH /api/v1/admin/tenants/{tenant_id}/users/{user_id}``.

    All fields optional — omitted fields are preserved. ``password`` is
    deliberately NOT exposed here (use the dedicated password-reset
    endpoint to avoid an accidental hash leak through the audit log).
    ``email`` cannot be changed (it is the natural login key; to
    rename, delete + recreate).
    """

    full_name: str | None = Field(default=None, max_length=200)
    role: UserRole | None = None
    is_active: bool | None = None


class UserPasswordReset(BaseModel):
    """Request body for ``POST .../users/{user_id}/reset-password``.

    Admin-triggered password reset. The new password is bcrypt-hashed
    server-side before insert; ``password_hash`` is never returned
    over the wire.
    """

    new_password: str = Field(..., min_length=8, max_length=128)


__all__ = [
    "UserCreate",
    "UserPasswordReset",
    "UserRead",
    "UserUpdate",
]