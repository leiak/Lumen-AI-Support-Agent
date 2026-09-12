"""WebSocket route for the agent workspace (Stage 9.5).

This is a minimal agent-facing WS endpoint that authenticates the
caller with their agent JWT and subscribes them to live message
events for a specific conversation.

Wire-protocol decision (Stage 9.5):
-----------------------------------
The frontend workspace needs real-time updates on conversation state.
The customer-facing widget already has a WS endpoint
(``/api/v1/widget/ws``) but it authenticates with a widget token
(``typ=widget``) bound to the customer's ``external_user_id`` and a
specific ``channel_id``. That endpoint cannot be reused for the agent
workspace for two reasons:

1. The agent does NOT hold a widget token — they authenticate with
   the standard JWT issued by ``POST /auth/login`` (no ``typ`` claim).
2. The widget endpoint is single-tenant-bound to ``channel_id``,
   which is correct for the customer surface but doesn't fit the
   agent model (an agent works across many channels).

So we add a parallel endpoint at
``/api/v1/agents/conversations/{conversation_id}/ws`` that:

* Authenticates with the agent JWT (the same one the workspace uses
  for REST). Token is supplied via ``?token=`` query parameter
  because WebSocket clients cannot set arbitrary headers — this
  mirrors the existing widget pattern at ``/api/v1/widget/ws``.
* Resolves the conversation → channel_id via the service layer and
  enforces tenant isolation (anti-enumeration: cross-tenant or
  unknown ids close with 1008, no payload).
* Registers the socket with the existing
  :class:`widget.ws.manager.ConnectionManager` keyed by ``channel_id``
  so it receives the same ``message.complete`` /
  ``message.created`` events the customer-facing widget sees.

No new broadcast paths are introduced — we only add a new
subscriber that joins the existing channel. The manager already
filters by ``channel_id`` and broadcasts to every subscriber on
that channel, so adding the agent socket is transparent to the
widget clients.

The endpoint is intentionally read-only: it ignores inbound
frames from the agent (the agent posts messages via REST, not via
WS). This keeps the surface tiny and avoids inventing a parallel
"agent WS protocol" for M1.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect, status

from auth.dependencies import _decode_jwt
from auth.jwt import TokenError
from conversation.service import ConversationService
from widget.ws.manager import manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/agents", tags=["agent-ws"])

__all__ = ["router", "websocket_endpoint"]


@router.websocket("/conversations/{conversation_id}/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    conversation_id: str,
    token: Annotated[str, Query(min_length=1)],
) -> None:
    """Authenticate the calling agent and stream live events for ``conversation_id``.

    Auth flow:
    1. Decode the agent JWT (same secret + algorithm as REST). 1008 on
       any failure — never echo the token or the reason.
    2. Enforce ``role in {agent, admin, owner}``.
    3. Resolve the conversation via :class:`ConversationService` with
       tenant scoping. Cross-tenant or unknown id → 1008 (same
       anti-enumeration contract the REST surface uses for 404).
    4. Look up the channel_id via the service (used as the manager
       broadcast key) and register the socket.

    On disconnect we always drop the registration. The manager
    silently swallows dead-socket broadcast errors so the agent's
    inbound REST POST never observes WS-related failures.
    """
    # 1. Decode JWT — same path as the REST ``require_agent_or_admin``
    # dependency, so the agent identity contract stays in one place.
    try:
        claims = await _decode_jwt(f"Bearer {token}")
    except TokenError as exc:
        logger.warning("agent ws: token rejected: %s", exc)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    except HTTPException:
        # ``_decode_jwt`` raises ``HTTPException(401)`` on bad token.
        # Translate that into the WS close code so the client sees a
        # normal close rather than a protocol-level hangup.
        logger.warning("agent ws: token rejected")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    role = claims.get("role")
    if role not in ("agent", "admin", "owner"):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    if not claims.get("sub") or not claims.get("tenant_id"):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    tenant_id = claims["tenant_id"]
    agent_id = claims["sub"]

    # 2. Resolve conversation → channel_id under tenant scope.
    conv = await ConversationService().get(
        tenant_id=tenant_id, conversation_id=conversation_id
    )
    if conv is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    channel_id = conv.channel_id

    # 3. Accept + register. Manager keys connections by channel_id
    # so we transparently join the existing broadcast group — no new
    # fanout code.
    await websocket.accept()
    connection_id = await manager.connect(
        websocket=websocket,
        channel_id=channel_id,
        tenant_id=tenant_id,
        external_user_id=agent_id,
    )
    logger.info(
        "agent ws: connected",
        extra={
            "conversation_id": conversation_id,
            "channel_id": channel_id,
            "tenant_id": tenant_id,
        },
    )
    try:
        # Read-only loop: drain client frames so the socket stays
        # alive across proxies that time out idle connections. We
        # don't process inbound frames — agent posts go through REST.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(connection_id)
