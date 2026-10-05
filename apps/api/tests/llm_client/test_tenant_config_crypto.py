"""Tests for TenantLLMConfigCipher (Fernet wrapper)."""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from llm_client.tenant_config_crypto import TenantLLMConfigCipher


def _gen_key() -> str:
    return Fernet.generate_key().decode("utf-8")


def test_encrypt_decrypt_roundtrip() -> None:
    cipher = TenantLLMConfigCipher(_gen_key())
    plaintext = "sk-test-12345"
    ciphertext = cipher.encrypt(plaintext)
    assert isinstance(ciphertext, bytes)
    assert cipher.decrypt(ciphertext) == plaintext


def test_decrypt_rejects_wrong_key() -> None:
    encrypter = TenantLLMConfigCipher(_gen_key())
    ciphertext = encrypter.encrypt("sk-original")
    wrong_cipher = TenantLLMConfigCipher(_gen_key())
    with pytest.raises(InvalidToken):
        wrong_cipher.decrypt(ciphertext)


def test_cipher_constructor_requires_key() -> None:
    with pytest.raises(RuntimeError, match="TENANT_LLM_FERNET_KEY"):
        TenantLLMConfigCipher(None)
    with pytest.raises(RuntimeError, match="TENANT_LLM_FERNET_KEY"):
        TenantLLMConfigCipher("")


def test_encrypted_bytes_are_not_plaintext() -> None:
    cipher = TenantLLMConfigCipher(_gen_key())
    plaintext = "sk-visible-secret-key"
    ciphertext = cipher.encrypt(plaintext)
    # Fernet = AES-128-CBC + HMAC-SHA256. The plaintext must not be
    # trivially recoverable from either the URL-safe base64 ciphertext
    # or the decoded token bytes (Fernet token layout: version + ts +
    # IV + AES-encrypted ciphertext + HMAC — none of which contain the
    # plaintext bytes).
    assert plaintext.encode("utf-8") not in ciphertext
    import base64
    decoded = base64.urlsafe_b64decode(ciphertext + b"=" * (-len(ciphertext) % 4))
    assert plaintext.encode("utf-8") not in decoded
