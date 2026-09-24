"""Neon Postgres access for the `user_settings` table: the list of email
addresses authorised to log into x402-analysis-gui, plus each one's role
(user_type) and theme preference (light_mode).

The `email` column is never stored in plaintext -- see gui/crypto.py.
Lookups go by `email_hash` (a keyed HMAC-SHA256 of the normalized email);
`email_encrypted` (AES-256-GCM) is only decrypted for display/audit
purposes -- the normal login/settings path never needs it, since the
session already carries the plaintext email from Auth0.

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

from gui.crypto import decrypt, encrypt, lookup_hash

USER_TYPE_ADMIN = 0
USER_TYPE_ADVANCED = 1
USER_TYPE_STANDARD = 2

USER_TYPE_NAMES = {
    USER_TYPE_ADMIN: "Admin",
    USER_TYPE_ADVANCED: "Advanced",
    USER_TYPE_STANDARD: "Standard",
}

LIGHT_MODE_AUTO = 0
LIGHT_MODE_LIGHT = 1
LIGHT_MODE_DARK = 2

VALID_LIGHT_MODES = {LIGHT_MODE_AUTO, LIGHT_MODE_LIGHT, LIGHT_MODE_DARK}
VALID_USER_TYPES = {USER_TYPE_ADMIN, USER_TYPE_ADVANCED, USER_TYPE_STANDARD}


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


def find_user_by_email(email: str) -> Optional[dict]:
    """{"user_type": int, "light_mode": int} for an authorised user, or
    None if this email isn't in user_settings."""
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT user_type, light_mode FROM user_settings WHERE email_hash = %s",
                (lookup_hash(email),),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def update_light_mode(email: str, light_mode: int) -> bool:
    """Updates the light_mode for an existing authorised user. Returns
    False if the email isn't in user_settings (nothing to update)."""
    if light_mode not in VALID_LIGHT_MODES:
        raise ValueError(f"invalid light_mode: {light_mode!r}")
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_settings SET light_mode = %s, updated_at = now() WHERE email_hash = %s",
                (light_mode, lookup_hash(email)),
            )
            updated = cur.rowcount > 0
    return updated


def add_user(email: str, user_type: int = USER_TYPE_STANDARD, light_mode: int = LIGHT_MODE_AUTO) -> None:
    """Adds a new authorised user, or updates user_type/light_mode (and
    re-encrypts the email) if that email is already present. Used by
    scripts/add_user.py -- the running web app never calls this itself,
    since it has no self-service "invite a user" feature."""
    if user_type not in VALID_USER_TYPES:
        raise ValueError(f"invalid user_type: {user_type!r}")
    if light_mode not in VALID_LIGHT_MODES:
        raise ValueError(f"invalid light_mode: {light_mode!r}")
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_settings (email_hash, email_encrypted, user_type, light_mode)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (email_hash) DO UPDATE
                    SET email_encrypted = EXCLUDED.email_encrypted,
                        user_type = EXCLUDED.user_type,
                        light_mode = EXCLUDED.light_mode,
                        updated_at = now()
                """,
                (lookup_hash(email), encrypt(email), user_type, light_mode),
            )


def remove_user(email: str) -> bool:
    """Removes an authorised user. Returns False if the email wasn't present."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_settings WHERE email_hash = %s", (lookup_hash(email),))
            deleted = cur.rowcount > 0
    return deleted


def list_users() -> list[dict]:
    """{"email": str, "user_type": int, "light_mode": int} for every
    authorised user, decrypting each stored email. Used by
    scripts/add_user.py --list, not by the running web app."""
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT email_encrypted, user_type, light_mode FROM user_settings ORDER BY created_at"
            )
            rows = cur.fetchall()
    return [
        {"email": decrypt(row["email_encrypted"]), "user_type": row["user_type"], "light_mode": row["light_mode"]}
        for row in rows
    ]
