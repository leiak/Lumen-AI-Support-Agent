"""FastAPI dependencies for authenticated routes."""
from typing import Annotated, Any

from fastapi import Header, HTTPException, status

from auth.jwt import TokenError, decode_token
from core.database import set_tenant_contextvar


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Validate the Bearer token from the Authorization header and return the JWT payload.

    Side effect: sets the tenant contextvar for downstream repository calls.
    Raises 401 on missing/malformed/invalid token.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization[len("Bearer "):]
    try:
        payload = decode_token(token)
    except TokenError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e
    set_tenant_contextvar(payload["tenant_id"])
    return payload
