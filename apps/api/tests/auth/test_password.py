"""Tests for bcrypt password hashing."""
from auth.password import hash_password, verify_password


def test_hash_and_verify_roundtrip() -> None:
    plain = "correct-horse-battery-staple"
    hashed = hash_password(plain)
    assert hashed != plain
    assert verify_password(plain, hashed) is True
    assert verify_password("wrong", hashed) is False


def test_hash_produces_unique_salts() -> None:
    """Same plain password should produce different hashes due to per-hash salt."""
    plain = "same-password"
    h1 = hash_password(plain)
    h2 = hash_password(plain)
    assert h1 != h2, "bcrypt should produce unique hashes per call (salt randomness)"
    assert verify_password(plain, h1)
    assert verify_password(plain, h2)
