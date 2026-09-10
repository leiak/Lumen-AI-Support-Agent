"""Pydantic models for auth API."""
from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    """POST /auth/login body."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class UserInfo(BaseModel):
    """Public-facing user info (no password hash)."""

    id: str
    tenant_id: str
    email: str
    full_name: str | None
    role: str


class LoginResponse(BaseModel):
    """POST /auth/login response."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105
    expires_in: int  # seconds
    user: UserInfo
