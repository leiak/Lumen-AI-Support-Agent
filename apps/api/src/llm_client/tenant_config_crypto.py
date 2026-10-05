"""Fernet-based encryption of tenant LLM API keys at rest.

The master key is loaded from ``settings.tenant_llm_fernet_key`` (env
``TENANT_LLM_FERNET_KEY``). Fernet = AES-128-CBC + HMAC-SHA256; safe to
store ciphertext in the DB without additional key wrapping. Generate
a development key with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class TenantLLMConfigCipher:
    """Encrypts / decrypts per-tenant LLM API keys using Fernet."""

    def __init__(self, master_key: str | None) -> None:
        if not master_key:
            raise RuntimeError(
                "TENANT_LLM_FERNET_KEY is required for M4.C BYOK. "
                "Generate with: "
                "python -c 'from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())'"
            )
        self._fernet = Fernet(master_key.encode("utf-8"))

    def encrypt(self, plaintext: str) -> bytes:
        """Encrypt an API key. Returns URL-safe base64 ciphertext bytes."""
        if not plaintext:
            raise ValueError("plaintext cannot be empty")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        """Decrypt ciphertext produced by :meth:`encrypt`.

        Raises:
            InvalidToken: master key changed since encryption. Operator
                must re-encrypt the row (no automatic rotation).
        """
        return self._fernet.decrypt(ciphertext).decode("utf-8")


__all__ = ["TenantLLMConfigCipher", "InvalidToken"]
