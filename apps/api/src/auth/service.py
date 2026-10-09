"""Authentication service: login flow with credential verification."""
from auth.jwt import create_access_token
from auth.password import hash_password, needs_rehash, verify_password
from auth.schemas import LoginResponse, UserInfo
from core.config import get_settings
from tenant.repository import TenantRepository, UserRepository


class InvalidCredentialsError(Exception):
    """Raised when login credentials are wrong or the user is inactive."""


class AuthService:
    """Orchestrates credential check + user lookup + token issuance."""

    def __init__(self) -> None:
        self.tenants = TenantRepository()
        self.users = UserRepository()

    async def login(self, tenant_id: str, email: str, password: str) -> LoginResponse:
        """Verify credentials and return a JWT + user info.

        Raises InvalidCredentialsError on:
        - user not found
        - user is inactive
        - password mismatch

        All three cases return the same error to avoid leaking which one was wrong.

        On a successful login, transparently upgrades the stored hash if
        it was made with an older bcrypt rounds value (12 -> 13). Failures
        during the upgrade are logged but do NOT block the login — the
        user can still authenticate, and the next successful login will
        retry.
        """
        user = await self.users.get_by_email(tenant_id, email)
        if user is None or not user.is_active:
            raise InvalidCredentialsError("Invalid credentials")
        if not verify_password(password, user.password_hash):
            raise InvalidCredentialsError("Invalid credentials")
        # Transparent bcrypt rounds upgrade: if the stored hash was made
        # with an older rounds value, re-hash with current rounds and
        # persist. Best-effort — a transient DB failure here shouldn't
        # block the user's login.
        if needs_rehash(user.password_hash):
            try:
                await self.users.update_password_hash(
                    user.id, hash_password(password)
                )
            except Exception as e:  # best-effort upgrade — don't block login
                from core.logging import get_logger

                get_logger("auth.service").warning(
                    "auth.password.rehash_failed",
                    user_id=user.id,
                    error=str(e),
                )
        settings = get_settings()
        token = create_access_token(tenant_id=tenant_id, user_id=user.id, role=str(user.role))
        return LoginResponse(
            access_token=token,
            expires_in=settings.jwt_access_token_ttl_minutes * 60,
            user=UserInfo(
                id=user.id,
                tenant_id=user.tenant_id,
                email=user.email,
                full_name=user.full_name,
                role=str(user.role),
            ),
        )