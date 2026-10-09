"""Tests for bcrypt password hashing."""
from passlib.context import CryptContext

from auth.password import hash_password, needs_rehash, verify_password


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


def test_needs_rehash_true_for_12_round_hash() -> None:
    """A 12-round hash should be flagged for upgrade to 13 rounds."""
    legacy = CryptContext(schemes=["bcrypt"], bcrypt__rounds=12)
    h = legacy.hash("hello")
    assert needs_rehash(h) is True


def test_needs_rehash_false_for_current_hash() -> None:
    """A hash made at the current rounds should NOT be flagged."""
    h = hash_password("hello")
    assert needs_rehash(h) is False