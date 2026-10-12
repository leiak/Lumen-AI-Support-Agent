"""Pydantic schemas for the M5.3 audit log GET endpoint.

Mirrors the ``{entries, total}`` envelope used by
``list_tenant_tickets`` (Tier 1 Task 1.1) so the SPA gets
paginated data + a total count without a second round-trip.

PII discipline
--------------

``changed_by`` is the user ULID (claims['sub']); we do NOT join to
``users`` here. The SPA can resolve ULID → display name via the
existing ``GET /admin/tenants/{id}/users`` endpoint if it needs
to render an "admin 张三 at 18:30" line. Keeping the audit response
single-table keeps it cheap to scan.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class TenantLLMConfigAuditEntry(BaseModel):
    """One row from ``tenant_llm_config_audit_log``."""

    id: str
    provider_name: str
    field: str
    old_value: str | None
    new_value: str | None
    changed_by: str
    changed_at: datetime


class TenantLLMConfigAuditListResponse(BaseModel):
    """Envelope for the GET audit endpoint."""

    entries: list[TenantLLMConfigAuditEntry]
    total: int


__all__ = [
    "TenantLLMConfigAuditEntry",
    "TenantLLMConfigAuditListResponse",
]