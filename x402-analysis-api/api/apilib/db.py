"""Neon Postgres access for x402-analysis-api.

The `api_keys` table -- who may call this server's API-key-protected
endpoints (see apilib/auth.py) -- plus the connection check `GET /status`
uses, plus read access to the x402 ecosystem data tables (facilitator,
uris, addresses, linkaddresses -- see db/schema.sql) for GET /facilitators
and GET /facilitators/{name}.

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

# linkaddresses.owner: matches the mapping documented in db/schema.sql.
OWNER_FACILITATOR = 0


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


def list_facilitators() -> list[dict]:
    """{"name", "risk", "active"} for every facilitator, ordered by name.
    Used by GET /facilitators."""
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT name, risk, active FROM facilitator ORDER BY name")
            rows = cur.fetchall()
    return [dict(row) for row in rows]


def _uri_row(cur, uri_id: Optional[int]) -> Optional[dict]:
    if uri_id is None:
        return None
    cur.execute("SELECT url, ip, location, subject, risk, notes, updated FROM uris WHERE id = %s", (uri_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def find_facilitator(name: str) -> Optional[dict]:
    """Every column on one facilitator row, plus its api/doc/website/x402scan
    URLs (the full uris row for each, not just the bare URL string -- IP,
    geolocation, and TLS subject are "information available about" it too)
    and every address linked to it (via linkaddresses, owner=0). `name` is
    matched case-insensitively. Returns None if no facilitator matches.
    Used by GET /facilitators/{name}."""
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, name, api, doc, website, x402scan, risk, active, notes, updated
                FROM facilitator
                WHERE lower(name) = lower(%s)
                """,
                (name,),
            )
            row = cur.fetchone()
            if not row:
                return None
            facilitator = dict(row)

            for field in ("api", "doc", "website", "x402scan"):
                facilitator[field] = _uri_row(cur, facilitator[field])

            cur.execute(
                """
                SELECT a.address, a.chains, a.source, a.risk, a.notes, a.updated
                FROM linkaddresses la
                JOIN addresses a ON a.id = la.address
                WHERE la.owner = %s AND la.ref = %s
                ORDER BY a.address
                """,
                (OWNER_FACILITATOR, facilitator["id"]),
            )
            facilitator["addresses"] = [dict(r) for r in cur.fetchall()]

    return facilitator
