"""Bcrypt password hashing wrapper around passlib.

Uses 13 rounds (~500ms on modern CPUs) — the 2024+ recommendation.
`needs_rehash` lets the login path transparently upgrade legacy 12-round
hashes on next successful authentication.
"""
from passlib.context import CryptContext

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=13)


def hash_password(plain: str) -> str:
    """Hash a plaintext password using bcrypt with a fresh salt (13 rounds)."""
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a stored hash. Returns False on any error."""
    return _pwd_context.verify(plain, hashed)


def needs_rehash(hashed: str) -> bool:
    """Return True if the stored hash was made with an older rounds value.

    Login paths SHOULD call this on every successful verification and
    re-`hash_password` the plaintext when True, persisting the new
    hash. This makes the 12 -> 13 migration transparent — users don't
    have to reset passwords, and there is no big-bang migration.
    """
    return _pwd_context.needs_update(hashed)