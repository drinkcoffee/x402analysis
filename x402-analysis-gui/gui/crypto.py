"""AES-256-GCM encryption for values stored in the Neon database, plus a
deterministic lookup hash for the email column.

AES-GCM uses a random nonce per encryption, so its ciphertext can't be used
directly as an equality-searchable key (the same email encrypts to a
different ciphertext every time). `user_settings` therefore stores two
things per user: `email_encrypted` (the AES-256-GCM ciphertext, for
display/audit) and `email_hash` (a deterministic, keyed HMAC-SHA256 of the
normalized email, for `WHERE email_hash = ...` lookups).

A single 256-bit master key (DB_ENCRYPTION_KEY) is never used directly for
two different purposes -- HKDF-SHA256 derives two independent subkeys from
it, one for AES-GCM encryption and one for the HMAC lookup hash, so this
follows standard key-separation practice rather than reusing one key for
two algorithms.

Env vars:
    DB_ENCRYPTION_KEY   required -- a base64-encoded 256-bit (32-byte) key.
                        Generate one with:
                            python -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
                        On Vercel, set this as a Project -> Settings ->
                        Environment Variable, not in a committed file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_NONCE_LENGTH = 12  # 96-bit nonce, the standard/recommended size for AES-GCM
_KEY_LENGTH = 32  # 256 bits

_ENCRYPTION_INFO = b"x402-analysis-gui:field-encryption:v1"
_LOOKUP_INFO = b"x402-analysis-gui:lookup-hash:v1"


def _master_key() -> bytes:
    encoded = os.getenv("DB_ENCRYPTION_KEY")
    if not encoded:
        raise RuntimeError("DB_ENCRYPTION_KEY is not set.")
    try:
        key = base64.b64decode(encoded, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("DB_ENCRYPTION_KEY is not valid base64.") from exc
    if len(key) != _KEY_LENGTH:
        raise RuntimeError(
            f"DB_ENCRYPTION_KEY must decode to {_KEY_LENGTH} bytes (256 bits); got {len(key)}."
        )
    return key


def _derive_subkey(info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=_KEY_LENGTH, salt=None, info=info).derive(_master_key())


def encrypt(plaintext: str) -> str:
    """AES-256-GCM encrypt `plaintext`. Returns base64(nonce || ciphertext+tag)."""
    key = _derive_subkey(_ENCRYPTION_INFO)
    nonce = secrets.token_bytes(_NONCE_LENGTH)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt(token: str) -> str:
    """Inverse of encrypt(). Raises on a bad key or tampered/corrupt token."""
    key = _derive_subkey(_ENCRYPTION_INFO)
    raw = base64.b64decode(token)
    nonce, ciphertext = raw[:_NONCE_LENGTH], raw[_NONCE_LENGTH:]
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")


def lookup_hash(value: str) -> str:
    """Deterministic HMAC-SHA256 hex digest of the normalized (trimmed,
    lowercased) value, keyed by a subkey independent from the encryption
    key. Used as an equality-searchable column standing in for the
    plaintext value, since the AES-GCM ciphertext can't be searched by
    value."""
    key = _derive_subkey(_LOOKUP_INFO)
    normalized = value.strip().lower().encode("utf-8")
    return hmac.new(key, normalized, hashlib.sha256).hexdigest()
