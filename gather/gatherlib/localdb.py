"""gather's local PostgreSQL database.

A real, separate PostgreSQL server -- its own data directory, initialized
with `initdb` the first time it's needed, and left running on its own port
(5434 by default; override with GATHER_DB_PORT) so it doesn't collide with
a system Postgres on 5432 or x402-analysis-api's local mirror on 5433.
Everything it creates lives under gather/localdb/, which is gitignored.

Requires the `initdb`/`pg_ctl`/`pg_isready` binaries from a PostgreSQL
installation (e.g. `brew install postgresql@18`), on PATH or at a common
Homebrew location -- this doesn't install PostgreSQL itself.

Adapted from x402-analysis-api/scripts/download_db.py's local-server
handling (copied, so gather doesn't depend on it), minus everything to do
with downloading from Neon.
"""

from __future__ import annotations

import getpass
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import psycopg2

from . import GATHER_ROOT

LOCAL_DB_ROOT = GATHER_ROOT / "localdb"
LOCAL_DATA_DIR = LOCAL_DB_ROOT / "pgdata"
LOCAL_LOG_FILE = LOCAL_DB_ROOT / "postgres.log"
DB_NAME = "x402_gather"
SCHEMA_PATH = GATHER_ROOT / "db" / "schema.sql"

# Common install locations for a Homebrew PostgreSQL that isn't (only)
# linked onto PATH, newest major version first.
_COMMON_PG_BIN_DIRS = [
    f"/opt/homebrew/opt/postgresql@{v}/bin" for v in (18, 17, 16, 15, 14)
] + [f"/usr/local/opt/postgresql@{v}/bin" for v in (18, 17, 16, 15, 14)]


def port() -> int:
    return int(os.getenv("GATHER_DB_PORT", "5434"))


def _pg_bin(name: str) -> str:
    """Absolute path to a PostgreSQL binary, preferring the newest version
    under the common Homebrew locations, then PATH."""
    for bin_dir in _COMMON_PG_BIN_DIRS:
        candidate = Path(bin_dir) / name
        if candidate.exists():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(
        f"couldn't find the '{name}' PostgreSQL binary on PATH or in any of "
        f"{_COMMON_PG_BIN_DIRS}. Install PostgreSQL (e.g. `brew install "
        "postgresql@18`) or add its bin/ directory to PATH."
    )


def db_url() -> str:
    return f"postgresql://{getpass.getuser()}@localhost:{port()}/{DB_NAME}"


def _maintenance_db_url() -> str:
    """The server's always-present `postgres` database -- for creating
    DB_NAME, since you can't connect to a database that doesn't exist."""
    return f"postgresql://{getpass.getuser()}@localhost:{port()}/postgres"


def _data_dir_initialized() -> bool:
    return (LOCAL_DATA_DIR / "PG_VERSION").exists()


def _server_running() -> bool:
    if not _data_dir_initialized():
        return False
    result = subprocess.run([_pg_bin("pg_ctl"), "-D", str(LOCAL_DATA_DIR), "status"], capture_output=True)
    return result.returncode == 0


def preflight_checks() -> list[str]:
    """Problems that would stop ensure_server_running() working: a missing
    PostgreSQL binary, or the port taken by something else."""
    problems: list[str] = []
    for name in ("initdb", "pg_ctl", "pg_isready"):
        try:
            _pg_bin(name)
        except RuntimeError as exc:
            problems.append(str(exc))
    if problems:
        return problems

    if not _server_running():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("localhost", port())) == 0:
                problems.append(
                    f"port {port()} is already in use by something else (not gather's "
                    "own Postgres, which isn't running). Set GATHER_DB_PORT to a "
                    "different port in gather/.env, or stop whatever's using it."
                )
    return problems


def _wait_until_ready(timeout: float = 15.0) -> None:
    pg_isready = _pg_bin("pg_isready")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if subprocess.run([pg_isready, "-p", str(port())], capture_output=True).returncode == 0:
            return
        time.sleep(0.3)
    raise RuntimeError(f"gather's Postgres on port {port()} did not become ready within {timeout}s")


def ensure_server_running() -> None:
    """Initializes the data directory if this is the very first run, then
    starts the server if it isn't running. Safe to call every time."""
    LOCAL_DB_ROOT.mkdir(parents=True, exist_ok=True)

    if not _data_dir_initialized():
        print(f"initializing a Postgres data directory at {LOCAL_DATA_DIR} ...", flush=True)
        subprocess.run(
            [
                _pg_bin("initdb"),
                "-D", str(LOCAL_DATA_DIR),
                "-U", getpass.getuser(),
                "--auth=trust",  # local-only database, never exposed off localhost
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    if not _server_running():
        print(f"starting gather's Postgres on port {port()} ...", flush=True)
        subprocess.run(
            [
                _pg_bin("pg_ctl"),
                "-D", str(LOCAL_DATA_DIR),
                "-o", f"-p {port()}",
                "-l", str(LOCAL_LOG_FILE),
                "start",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        _wait_until_ready()


def database_exists() -> bool:
    conn = psycopg2.connect(_maintenance_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def create_database() -> None:
    """Creates DB_NAME and its tables (db/schema.sql)."""
    conn = psycopg2.connect(_maintenance_db_url())
    conn.autocommit = True  # CREATE DATABASE can't run inside a transaction
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{DB_NAME}"')
    finally:
        conn.close()

    conn = psycopg2.connect(db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_PATH.read_text())
        conn.commit()
    finally:
        conn.close()


def ensure_schema(cur) -> None:
    """Creates any table in db/schema.sql the database doesn't have yet --
    e.g. one added to the schema after 00_init_db.py ran. Every statement
    there is CREATE TABLE IF NOT EXISTS, so existing tables are untouched."""
    cur.execute(SCHEMA_PATH.read_text())


def connect_existing():
    """Starts the server if needed and connects to the database, exiting
    with a message if 00_init_db.py hasn't created it yet."""
    ensure_server_running()
    if not database_exists():
        print(
            f"gather's database ({DB_NAME}) doesn't exist yet -- run `python gather/00_init_db.py` first.",
            file=sys.stderr,
        )
        sys.exit(2)
    return psycopg2.connect(db_url())
