"""Feishu (Lark) webhook signature verification + encrypted-event decryption.

Reference: https://open.feishu.cn/document/server-docs/event-subscription-guide

Two security modes:
1. Encrypted mode: app has `encrypt_key` set. Feishu sends AES-256-CBC encrypted
   events; we must decrypt them before parsing.
2. Plain mode: no encryption. Events are plain JSON.

Both modes require signature verification using the `encrypt_key`
even in plain mode. Feishu's signing scheme is a custom SHA-256 digest over the
concatenation `timestamp + nonce + encrypt_key + body` (this is NOT HMAC;
the key is embedded in the message string, not used as HMAC's key argument).
"""
import base64
import hashlib
import hmac
import time
from typing import Final

from cryptography.hazmat.primitives import padding as crypto_padding
from cryptography.hazmat.primitives.ciphers import Cipher as CryptoCipher
from cryptography.hazmat.primitives.ciphers import algorithms, modes

DEFAULT_TIMESTAMP_MAX_AGE_SECONDS: Final[int] = 300  # 5 minutes


class FeishuDecryptionError(ValueError):
    """Raised when an encrypted Feishu event cannot be decoded/decrypted.

    Inherits from ValueError so existing callers that catch ValueError still
    work, while allowing HTTP layers to map this specific failure to 400.
    """


def _derive_aes_key(encrypt_key: str) -> bytes:
    """Derive the 32-byte AES-256 key from the Feishu encrypt_key string.

    Feishu uses the encrypt_key as the AES key directly, padded/truncated to 32 bytes.
    """
    raw = encrypt_key.encode("utf-8")
    if len(raw) >= 32:
        return raw[:32]
    return raw + b"\x00" * (32 - len(raw))


def sign_feishu_payload(
    *,
    timestamp: str,
    nonce: str,
    encrypt_key: str,
    body: bytes,
) -> str:
    """Compute the Feishu signature for a webhook payload.

    Feishu's signing algorithm: SHA-256(timestamp + nonce + encrypt_key + body)
    Returns the hex digest (64 chars).
    """
    string_to_sign = timestamp + nonce + encrypt_key + body.decode("utf-8")
    digest = hashlib.sha256(string_to_sign.encode("utf-8")).hexdigest()
    return digest


def verify_feishu_signature(
    *,
    timestamp: str,
    nonce: str,
    encrypt_key: str,
    body: bytes,
    signature: str,
) -> bool:
    """Constant-time check that `signature` matches the expected SHA-256 digest."""
    try:
        expected = sign_feishu_payload(
            timestamp=timestamp,
            nonce=nonce,
            encrypt_key=encrypt_key,
            body=body,
        )
    except UnicodeDecodeError:
        # Body is not valid UTF-8 → treat as a signature mismatch rather than
        # propagating an exception that would bypass the security check.
        return False
    return hmac.compare_digest(expected, signature)


def verify_timestamp_freshness(
    timestamp: str,
    *,
    max_age_seconds: int = DEFAULT_TIMESTAMP_MAX_AGE_SECONDS,
) -> bool:
    """Return True if `timestamp` (seconds since epoch) is within max_age_seconds of now.

    Also returns False if the timestamp is malformed.
    """
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False
    return abs(time.time() - ts) <= max_age_seconds


def decrypt_feishu_event(
    *,
    encrypted: str,
    iv: str,
    encrypt_key: str,
) -> str:
    """Decrypt a Feishu-encrypted event body. AES-256-CBC + PKCS#7 padding.

    Feishu's encrypted event wraps the inner JSON in this envelope:
    {"encrypt": "<base64-ciphertext>", "iv": "<base64-iv>"}
    Both `encrypted` and `iv` are passed in as kwargs.
    """
    key_bytes = _derive_aes_key(encrypt_key)
    ciphertext = base64.b64decode(encrypted)
    iv_bytes = base64.b64decode(iv)

    cipher = CryptoCipher(algorithms.AES(key_bytes), modes.CBC(iv_bytes))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = crypto_padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Decryption succeeded but the plaintext is not valid UTF-8 → treat
        # as a malformed event payload so HTTP layer can return 400 instead
        # of letting the exception crash the request handler.
        raise FeishuDecryptionError("decrypted payload is not valid UTF-8") from exc
