"""API-key authentication for x402-analysis-api.

Two access levels, both rows in the `api_keys` table (see db/schema.sql) --
there's no self-service key creation; see scripts/create_api_key.py to mint
one.

    read         can call read-only endpoints
    read_write   can call read-only AND read-write endpoints

Callers send their key in the `X-API-Key` header. Keys are stored hashed
(SHA-256, see hash_api_key) rather than in plaintext -- since a key is a
high-entropy random token rather than a guessable password, an unsalted
hash is sufficient here (no rainbow-table risk the way there would be for a
low-entropy secret).

`GET /` and `GET /status` (api/app.py) don't use either dependency below --
they're intentionally reachable with no key at all.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from fastapi import Header, HTTPException, status

import apilib.db as db

API_KEY_HEADER = "X-API-Key"

ACCESS_READ = "read"
ACCESS_READ_WRITE = "read_write"

# read_write implies read: a read_write key must satisfy a `require_read_access`
# check too, not just its own level.
_LEVEL_RANK = {ACCESS_READ: 0, ACCESS_READ_WRITE: 1}


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _require_access(min_level: str):
    async def dependency(x_api_key: Optional[str] = Header(default=None, alias=API_KEY_HEADER)) -> dict:
        if not x_api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Missing {API_KEY_HEADER} header.",
            )
        row = db.find_api_key(hash_api_key(x_api_key))
        if not row:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key.")
        if _LEVEL_RANK[row["access_level"]] < _LEVEL_RANK[min_level]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This API key does not have sufficient access for this endpoint.",
            )
        return row

    return dependency


# FastAPI route dependencies: Depends(require_read_access) / Depends(require_read_write_access).
require_read_access = _require_access(ACCESS_READ)
require_read_write_access = _require_access(ACCESS_READ_WRITE)
