#!/usr/bin/env python3
"""Creates gather's local Postgres database, with the tables from
db/schema.sql.

Starts gather's own Postgres server first (initializing its data directory
under gather/localdb/ on the very first run) -- see gatherlib/localdb.py.
Exits with an error, changing nothing, if the database already exists.

Usage:
    python gather/00_init_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

GATHER_ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    import psycopg2  # noqa: F401
except ImportError as exc:
    print(
        f"missing required package: {exc.name}. Run `pip install -r gather/requirements.txt`, "
        "with gather's virtualenv active.",
        file=sys.stderr,
    )
    sys.exit(1)

load_dotenv(GATHER_ROOT / ".env")

from gatherlib import localdb, preflight  # noqa: E402


def main() -> None:
    preflight.check_or_exit(localdb.preflight_checks)
    localdb.ensure_server_running()

    if localdb.database_exists():
        print(
            f"the database {localdb.DB_NAME} already exists (port {localdb.port()}) -- nothing done. "
            f"To start again from scratch, drop it first: "
            f"`dropdb -p {localdb.port()} {localdb.DB_NAME}`.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"creating the database {localdb.DB_NAME} ...", flush=True)
    localdb.create_database()
    print(f"created {localdb.db_url()} with the tables from {localdb.SCHEMA_PATH.relative_to(GATHER_ROOT)}")


if __name__ == "__main__":
    main()
