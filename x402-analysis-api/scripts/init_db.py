#!/usr/bin/env python3
"""Creates the api_keys table in your Neon database, from db/schema.sql.

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

SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"


def main() -> None:
    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_PATH.read_text())
        conn.commit()
    finally:
        conn.close()
    print("Tables are ready.")


if __name__ == "__main__":
    main()
