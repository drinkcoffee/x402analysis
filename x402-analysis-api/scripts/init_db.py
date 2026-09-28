#!/usr/bin/env python3
"""Creates (or updates) every table in your Neon database, from
db/schema.sql and db/migrations.sql.

schema.sql's `CREATE TABLE IF NOT EXISTS` only handles tables that don't
exist yet -- it can't add a column or constraint to a table that was
created before that column/constraint was added to schema.sql. migrations.sql
covers that: a set of idempotent ALTER statements safe to run any number of
times (including against a database that's already fully up to date, where
they're a no-op). Running both here means one command handles a brand-new
database and patching up an existing one alike.

Usage:
    python scripts/init_db.py

Requires DATABASE_URL, read from a .env file in the project root (if
present) or the real environment.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

import psycopg2  # noqa: E402

from db_setup import ensure_schema  # noqa: E402


def main() -> None:
    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            ensure_schema(cur)
        conn.commit()
    finally:
        conn.close()
    print("Tables are ready.")


if __name__ == "__main__":
    main()
