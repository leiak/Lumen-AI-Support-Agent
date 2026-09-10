"""Authentication HTTP endpoints."""
from fastapi import APIRouter, Header, HTTPException, status

from auth.schemas import LoginRequest, LoginResponse
from auth.service import AuthService, InvalidCredentialsError

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


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
