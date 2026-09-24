#!/usr/bin/env python3
"""Creates the user_settings table in the Neon database pointed to by
DATABASE_URL, if it doesn't already exist. Safe to re-run.

Usage:
    python scripts/init_db.py

Reads DATABASE_URL from a .env file in the project root (if present) or the
real environment.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import psycopg2  # noqa: E402

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def main() -> None:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set (checked .env and the environment).")

    sql = SCHEMA_PATH.read_text()
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        conn.close()
    print("user_settings table is ready.")


if __name__ == "__main__":
    main()
