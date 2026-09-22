"""Bearer JWT generation for Coinbase CDP Platform API v2 authentication.

Per https://docs.cdp.coinbase.com/api-reference/v2/authentication:
  - Header: {"alg": <EdDSA|ES256>, "typ": "JWT", "kid": <key id>, "nonce": <random hex>}
  - Claims: iss="cdp", sub=<key id>, aud=["cdp_service"], nbf, exp (nbf+120s),
    uri="{METHOD} {HOST}{PATH}"
  - Signing key: newer CDP API keys are Ed25519 (base64-encoded 64-byte
    seed+pubkey, or 32-byte seed) signed with EdDSA. Legacy keys are an EC
    private key in PEM form, signed with ES256.
"""

from __future__ import annotations

import base64
import secrets
import time

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

CDP_JWT_ISSUER = "cdp"
CDP_JWT_AUDIENCE = ["cdp_service"]
CDP_JWT_TTL_SECONDS = 120


class CdpAuthError(Exception):
    pass


def _load_signing_key(key_secret: str):
    """Return (key, algorithm) for the given CDP API key secret."""
    secret = key_secret.strip()
    if secret.startswith("-----BEGIN"):
        return secret, "ES256"

    try:
        raw = base64.b64decode(secret, validate=True)
    except (ValueError, TypeError) as exc:
        raise CdpAuthError(
            "CDP API key secret is neither a PEM EC private key nor valid "
            "base64."
        ) from exc

    if len(raw) == 64:
        seed = raw[:32]
    elif len(raw) == 32:
        seed = raw
    else:
        raise CdpAuthError(
            f"Unrecognized CDP API key secret length ({len(raw)} bytes); "
            "expected a 32 or 64-byte Ed25519 key, or a PEM EC private key."
        )
    return Ed25519PrivateKey.from_private_bytes(seed), "EdDSA"


def build_bearer_token(
    key_id: str,
    key_secret: str,
    method: str,
    host: str,
    path: str,
) -> str:
    """Build a short-lived (120s) CDP bearer JWT for one request."""
    key, algorithm = _load_signing_key(key_secret)
    now = int(time.time())
    claims = {
        "sub": key_id,
        "iss": CDP_JWT_ISSUER,
        "aud": CDP_JWT_AUDIENCE,
        "nbf": now,
        "exp": now + CDP_JWT_TTL_SECONDS,
        "uri": f"{method.upper()} {host}{path}",
    }
    headers = {
        "kid": key_id,
        "nonce": secrets.token_hex(16),
    }
    return jwt.encode(claims, key, algorithm=algorithm, headers=headers)
