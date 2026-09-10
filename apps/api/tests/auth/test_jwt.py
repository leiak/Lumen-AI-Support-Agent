"""Tests for JWT encode/decode."""
import pytest

from auth.jwt import TokenError, create_access_token, decode_token


def test_jwt_roundtrip() -> None:
    token = create_access_token(tenant_id="t1", user_id="u1", role="admin")
    payload = decode_token(token)
    assert payload["sub"] == "u1"
    assert payload["tenant_id"] == "t1"
    assert payload["role"] == "admin"


def test_jwt_invalid_signature_rejected() -> None:
    token = create_access_token(tenant_id="t1", user_id="u1", role="admin")
    bad = token[:-2] + "xx"
    with pytest.raises(TokenError, match="Invalid token"):
        decode_token(bad)


def test_jwt_extra_claims_preserved() -> None:
    token = create_access_token(
        tenant_id="t1", user_id="u1", role="agent", extra={"scope": "channel:web"}
    )
    payload = decode_token(token)
    assert payload["scope"] == "channel:web"
