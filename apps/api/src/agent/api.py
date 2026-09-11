"""Agent workspace HTTP API (Stage 8.1).

This router owns the agent-facing identity endpoint used by the
workspace frontend's top-bar:

- ``GET /api/v1/agents/me`` — return the JWT-derived caller identity,
  augmented with the tenant's display name from ``TenantRepository``.

The agent reply endpoint (POST /conversations/{id}/messages) lives on
the conversation router to keep its canonical URL
``/api/v1/conversations/{id}/messages`` and so the existing
anti-enumeration + tenant-isolation story stays in one place.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from agent.schemas import AgentMeOut
from auth.dependencies import require_agent_or_admin
from core.logging import get_logger
from tenant.repository import TenantRepository

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

log = get_logger(__name__)


@router.get("/me", response_model=AgentMeOut)
async def get_me(
    claims: Annotated[dict[str, Any], Depends(require_agent_or_admin)],
) -> AgentMeOut:
    """Return the caller's identity (JWT-derived).

    The JWT carries ``sub`` (user_id), ``tenant_id``, ``role`` and
    ``email`` (when issued via the auth service). The human-readable
    ``tenant_name`` is looked up by id from ``TenantRepository`` and
    falls back to the tenant id if the lookup fails — never leaks an
    error to the caller (anti-enumeration parity with the rest of the
    workspace API).
    """
    tenant_id = claims["tenant_id"]
    user_id = claims["sub"]
    role = claims.get("role", "agent")
    email = str(claims.get("email", ""))
    tenant = await TenantRepository().get_by_id(tenant_id)
    tenant_name = tenant.name if tenant is not None else tenant_id
    # PII-safe log per Stage 8.1 spec: only user_id + tenant_id.
    # `role` is on the response body but intentionally NOT logged.
    log.info(
        "agent identity fetched",
        user_id=user_id,
        tenant_id=tenant_id,
    )
    return AgentMeOut(
        user_id=user_id,
        email=email,
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        role=role,
    )
