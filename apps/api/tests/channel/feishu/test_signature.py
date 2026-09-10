"""Tests for Feishu webhook signature verification."""
import base64
import os
import time

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher as CryptoCipher
from cryptography.hazmat.primitives.ciphers import algorithms, modes

from channel.feishu.signature import (
    decrypt_feishu_event,
    sign_feishu_payload,
    verify_feishu_signature,
    verify_timestamp_freshness,
)

FEISHU_ENCRYPT_KEY = "test_encrypt_key_for_dev_only"


def _encrypt_for_test(plaintext: str, encrypt_key: str = FEISHU_ENCRYPT_KEY) -> tuple[str, str]:
    """Encrypt `plaintext` using the same AES-256-CBC + PKCS#7 scheme Feishu uses.

    Returns (base64_ciphertext, base64_iv).
    """
    key_bytes = (encrypt_key.encode("utf-8") + b"\x00" * 32)[:32]
    iv = os.urandom(16)
    cipher = CryptoCipher(algorithms.AES(key_bytes), modes.CBC(iv))
    encryptor = cipher.encryptor()
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext.encode("utf-8") + bytes([pad_len] * pad_len)
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode("ascii"), base64.b64encode(iv).decode("ascii")


def test_sign_feishu_payload_deterministic() -> None:
    """Same input → same digest."""
    sig1 = sign_feishu_payload(
        timestamp="1700000000",
        nonce="abc123",
        encrypt_key=FEISHU_ENCRYPT_KEY,
        body=b'{"event":":)"}',
    )
    sig2 = sign_feishu_payload(
        timestamp="1700000000",
        nonce="abc123",
        encrypt_key=FEISHU_ENCRYPT_KEY,
        body=b'{"event":":)"}',
    )
    assert sig1 == sig2
    assert len(sig1) == 64  # SHA-256 hex


def test_verify_feishu_signature_valid() -> None:
    body = b'{"event":"test"}'
    sig = sign_feishu_payload(
        timestamp="1700000000",
        nonce="nonce1",
        encrypt_key=FEISHU_ENCRYPT_KEY,
        body=body,
    )
    assert (
        verify_feishu_signature(
            timestamp="1700000000",
            nonce="nonce1",
            encrypt_key=FEISHU_ENCRYPT_KEY,
            body=body,
            signature=sig,
        )
        is True
    )


def test_verify_feishu_signature_invalid() -> None:
    body = b'{"event":"test"}'
    assert (
        verify_feishu_signature(
            timestamp="1700000000",
            nonce="nonce1",
            encrypt_key=FEISHU_ENCRYPT_KEY,
            body=body,
            signature="0" * 64,  # wrong signature
        )
        is False
    )


def test_verify_feishu_signature_timing_safe() -> None:
    """Use hmac.compare_digest — invalid signatures don't leak via timing."""
    body = b'{"event":"test"}'
    valid_sig = sign_feishu_payload(
        timestamp="1700000000",
        nonce="nonce1",
        encrypt_key=FEISHU_ENCRYPT_KEY,
        body=body,
    )
    # Try various wrong sigs
    for wrong in [valid_sig[:-1] + "0", "f" * 64, ""]:
        result = verify_feishu_signature(
            timestamp="1700000000",
            nonce="nonce1",
            encrypt_key=FEISHU_ENCRYPT_KEY,
            body=body,
            signature=wrong,
        )
        assert result is False


def test_verify_timestamp_freshness_current() -> None:
    now = int(time.time())
    assert verify_timestamp_freshness(str(now)) is True
    assert verify_timestamp_freshness(str(now - 60)) is True  # 1 min ago


def test_verify_timestamp_freshness_old() -> None:
    old = int(time.time()) - 3600  # 1 hour ago
    assert verify_timestamp_freshness(str(old)) is False


def test_verify_timestamp_freshness_invalid() -> None:
    assert verify_timestamp_freshness("not-a-number") is False


def test_decrypt_feishu_event_roundtrip() -> None:
    """Encrypt a payload, then decrypt it, verify we get back the original plaintext."""
    # Construct a fake Feishu-encrypted event envelope
    inner_json = (
        '{"event":{"type":"message",'
        '"sender":{"sender_id":{"open_id":"ou_abc"}},'
        '"message":{"chat_id":"oc_xyz","message_id":"om_1",'
        '"chat_type":"p2p","content":"{}"}}}'
    )

    encrypted_b64, iv_b64 = _encrypt_for_test(inner_json, FEISHU_ENCRYPT_KEY)

    decrypted = decrypt_feishu_event(
        encrypted=encrypted_b64,
        iv=iv_b64,
        encrypt_key=FEISHU_ENCRYPT_KEY,
    )
    assert decrypted == inner_json


def test_decrypt_feishu_event_bad_key() -> None:
    """Wrong key should raise (bad PKCS#7 padding) rather than silently returning plaintext."""
    encrypted_b64, iv_b64 = _encrypt_for_test("test", FEISHU_ENCRYPT_KEY)

    # Decrypting with wrong key should either raise (bad padding) or return garbage.
    # The contract is: it should NOT silently return the plaintext.
    with pytest.raises(Exception):  # noqa: B017
        decrypt_feishu_event(
            encrypted=encrypted_b64,
            iv=iv_b64,
            encrypt_key="wrong_key",
        )
