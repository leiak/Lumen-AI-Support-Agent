"""Bcrypt password hashing wrapper around passlib.

Uses 12 rounds (~250ms on modern CPUs) — slow enough to deter brute force,
fast enough to not annoy users.
"""
from passlib.context import CryptContext

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(plain: str) -> str:
    """Hash a plaintext password using bcrypt with a fresh salt."""
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a stored hash. Returns False on any error."""
    return _pwd_context.verify(plain, hashed)
