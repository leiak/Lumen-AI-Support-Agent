"""FastAPI dependencies for authenticated routes.

This module is the single source of truth for JWT-based auth dependencies.
Any consumer (REST routes, WebSocket handlers, internal call sites) should
import the role gates from here so the decode/enforce logic lives in one
place.
"""
from typing import Annotated, Any

from fastapi import Header, HTTPException, status

from auth.jwt import TokenError, decode_token
from core.database import set_tenant_contextvar


async def _decode_jwt(authorization: str | None) -> dict[str, Any]:
    """Decode Bearer JWT or raise 401.

    Shared by every JWT-based dependency below. Kept private so callers
    cannot bypass the role gates.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[len("Bearer "):]
    try:
        return decode_token(token)
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Validate the Bearer token from the Authorization header and return the JWT payload.

    Side effect: sets the tenant contextvar for downstream repository calls.
    Raises 401 on missing/malformed/invalid token.
    """
    claims = await _decode_jwt(authorization)
    set_tenant_contextvar(claims["tenant_id"])
    return claims


async def require_admin(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """JWT auth — admin or owner role required.

    Returns the JWT claims dict (with ``sub``/``tenant_id``/``role``) on
    success. Raises 401 for missing/malformed/invalid tokens and 403 for
    authenticated callers without the admin/owner role. Side effect:
    sets the tenant contextvar.
    """
    claims = await _decode_jwt(authorization)
    role = claims.get("role")
    if role not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="admin role required")
    if "tenant_id" not in claims:
        raise HTTPException(status_code=401, detail="token missing tenant_id")
    set_tenant_contextvar(claims["tenant_id"])
    return claims


async def require_agent_or_admin(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """JWT auth — agent, admin, or owner allowed.

    Returns the JWT claims dict on success. Raises 401 for missing /
    malformed / invalid tokens and 403 for authenticated callers without
    a supported role. Side effect: sets the tenant contextvar.
    """
    claims = await _decode_jwt(authorization)
    role = claims.get("role")
    if role not in ("agent", "admin", "owner"):
        raise HTTPException(status_code=403, detail="agent or admin role required")
    if "tenant_id" not in claims:
        raise HTTPException(status_code=401, detail="token missing tenant_id")
    if not claims.get("sub"):
        raise HTTPException(status_code=401, detail="token missing sub claim")
    set_tenant_contextvar(claims["tenant_id"])
    return claims


__all__ = ["get_current_user", "require_admin", "require_agent_or_admin"]
