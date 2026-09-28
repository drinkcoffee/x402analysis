"""Neon Postgres access for x402-analysis-api.

Currently just the `api_keys` table -- who may call this server's
API-key-protected endpoints (see apilib/auth.py) -- plus the connection check
`GET /status` uses.

Each function opens and closes its own connection rather than pooling
in-process: Vercel serverless functions are short-lived and stateless, so
in-process pooling wouldn't help -- point DATABASE_URL at Neon's *pooled*
connection string (the one with "-pooler" in the hostname, from the Neon
console's Connection Details) so Neon's own PgBouncer absorbs the
connect/disconnect churn instead.

Env vars:
    DATABASE_URL   required -- a Neon Postgres connection string, e.g.
                  postgresql://user:password@ep-xxxx-pooler.region.aws.neon.tech/neondb?sslmode=require
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg2
import psycopg2.extensions
import psycopg2.extras

VALID_ACCESS_LEVELS = {"read", "read_write"}


def _connection_string() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set.")
    return url


@contextmanager
def _connect() -> Iterator[psycopg2.extensions.connection]:
    conn = psycopg2.connect(_connection_string())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def check_connection() -> None:
    """Raises if Neon can't be reached or doesn't respond; returns normally
    if it can. Used by GET /status."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()


def find_api_key(key_hash: str) -> Optional[dict]:
    """{"access_level": str} for a known key hash, or None if that key
    doesn't exist (or was revoked)."""
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT access_level FROM api_keys WHERE key_hash = %s", (key_hash,))
            row = cur.fetchone()
    return dict(row) if row else None


def add_api_key(key_hash: str, access_level: str, label: Optional[str] = None) -> None:
    """Stores a new API key's hash. Used by scripts/create_api_key.py --
    the running server has no self-service "mint a key" endpoint."""
    if access_level not in VALID_ACCESS_LEVELS:
        raise ValueError(f"invalid access_level: {access_level!r}")
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys (key_hash, access_level, label) VALUES (%s, %s, %s)",
                (key_hash, access_level, label),
            )


def revoke_api_key(key_id: int) -> bool:
    """Deletes an API key by id. Returns False if that id wasn't present."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM api_keys WHERE id = %s", (key_id,))
            deleted = cur.rowcount > 0
    return deleted


def list_api_keys() -> list[dict]:
    """{"id", "access_level", "label", "created_at"} for every API key --
    never the raw key or its hash, since the point is you can't recover a
    raw key anyway. Used by scripts/create_api_key.py --list."""
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, access_level, label, created_at FROM api_keys ORDER BY created_at")
            rows = cur.fetchall()
    return [dict(row) for row in rows]
