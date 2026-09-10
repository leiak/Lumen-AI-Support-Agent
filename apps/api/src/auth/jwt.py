"""JWT issuance and validation.

Tokens carry:
- sub: user id
- tenant_id: tenant context
- role: user role (admin / supervisor / agent / viewer)
- iat, exp: issued-at and expiry timestamps

HS256 by default. Settings provides jwt_secret and jwt_algorithm.
"""
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt

from core.config import get_settings


class TokenError(Exception):
    """Raised when a JWT cannot be decoded (invalid signature, expired, malformed)."""


def create_access_token(
    *,
    tenant_id: str,
    user_id: str,
    role: str,
    extra: dict[str, Any] | None = None,
) -> str:
    """Issue a signed JWT for the given tenant+user. `extra` is merged into the payload."""
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_access_token_ttl_minutes)).timestamp()),
    }
    if extra:
        payload.update(extra)
    encoded: str = jwt.encode(
        payload, settings.jwt_secret, algorithm=settings.jwt_algorithm
    )
    return encoded


def decode_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT. Raises TokenError on any failure."""
    settings = get_settings()
    try:
        return dict(jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]))
    except JWTError as e:
        raise TokenError(f"Invalid token: {e}") from e
