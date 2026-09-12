"""Authentication HTTP endpoints."""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import EmailStr

from auth.rate_limit import check_tenant_lookup_rate_limit, make_rate_limit_dependency
from auth.schemas import LoginRequest, LoginResponse, TenantHintResponse
from auth.service import AuthService, InvalidCredentialsError
from core.logging import get_logger
from tenant.models import Tenant, User
from tenant.repository import TenantRepository, UserRepository

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_log = get_logger("auth.api")

# Module-level Query() so the default isn't a function call inside the
# function signature (ruff B008). FastAPI still recognises it as the
# parameter's default at runtime.
_EMAIL_QUERY = Query(
    ..., description="Email address to resolve a tenant for."
)


def _email_hash(email: str) -> str:
    """Return a short, opaque, non-reversible identifier for an email.

    Used for debugging / audit logs only — NEVER log the raw address.
    """
    return hashlib.sha256(email.lower().strip().encode("utf-8")).hexdigest()[:8]


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-Id"),
) -> LoginResponse:
    """Authenticate a user and return a JWT.

    The X-Tenant-Id header scopes the email lookup to one tenant —
    cross-tenant login attempts must explicitly provide that header.
    """
    try:
        return await AuthService().login(x_tenant_id, payload.email, payload.password)
    except InvalidCredentialsError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e


# Rate-limit dependency wired here (rather than at module import time) so
# tests can monkeypatch `check_tenant_lookup_rate_limit` cleanly.
_tenant_lookup_rl_dep = make_rate_limit_dependency(check_tenant_lookup_rate_limit)


@router.get(
    "/lookup-tenant",
    response_model=TenantHintResponse,
    dependencies=[Depends(_tenant_lookup_rl_dep)],
)
async def lookup_tenant(
    request: Request,
    email: EmailStr = _EMAIL_QUERY,
) -> TenantHintResponse:
    """Return the tenant that owns ``email``, or nulls if none exists.

    Anti-enumeration: this endpoint **always returns HTTP 200** with the
    same JSON shape. A missing user and a hit both produce
    ``{tenant_id, tenant_name}`` — only the values differ. Combined with
    the per-IP rate limit (see ``auth.rate_limit``), this makes the
    endpoint useless as a vector for discovering registered emails.

    If a single email happens to exist in multiple tenants (rare — the
    schema enforces uniqueness per tenant), the lowest ``User.id``
    (ULID-ordered) wins deterministically so the response is stable
    for a given input.
    """
    users = UserRepository()
    tenants = TenantRepository()
    user: User | None = await users.get_first_by_email(email)

    if user is None or not user.is_active:
        _log.info(
            "auth.lookup_tenant.miss",
            email_hash=_email_hash(email),
            client_ip=request.client.host if request.client else None,
        )
        return TenantHintResponse(tenant_id=None, tenant_name=None)

    tenant: Tenant | None = await tenants.get_by_id(user.tenant_id)
    # Tenant row gone (shouldn't happen — FK + cascade) or tenant not ACTIVE
    if tenant is None or tenant.status != "active":
        _log.info(
            "auth.lookup_tenant.inactive_tenant",
            email_hash=_email_hash(email),
            tenant_id=user.tenant_id,
        )
        return TenantHintResponse(tenant_id=None, tenant_name=None)

    _log.info(
        "auth.lookup_tenant.hit",
        email_hash=_email_hash(email),
        tenant_id=tenant.id,
    )
    return TenantHintResponse(tenant_id=tenant.id, tenant_name=tenant.name)