"""Shared helper: applies db/schema.sql then db/migrations.sql against an
open cursor, bringing the database up to date regardless of its starting
state (brand new, already up to date, or an older shape missing some
column/constraint added since it was first created).

Used by both scripts/init_db.py and scripts/load_facilitators.py, so
loading data never hits a database that's a migration behind -- that's
exactly what caused `psycopg2.errors.InvalidColumnReference: there is no
unique or exclusion constraint matching the ON CONFLICT specification` the
first time: schema.sql had gained a UNIQUE constraint load_facilitators.py
needed, but the already-created table didn't have it, and re-running
load_facilitators.py alone can't fix that -- only re-running init_db.py
(or, now, running load_facilitators.py itself) applies it.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"
MIGRATIONS_PATH = PROJECT_ROOT / "db" / "migrations.sql"


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA_PATH.read_text())
    cur.execute(MIGRATIONS_PATH.read_text())
