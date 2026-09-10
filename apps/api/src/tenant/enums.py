"""Tenant-domain enums (plan, user role, status)."""
from enum import StrEnum


class TenantPlan(StrEnum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class UserRole(StrEnum):
    ADMIN = "admin"
    SUPERVISOR = "supervisor"
    AGENT = "agent"
    VIEWER = "viewer"


class TenantStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"