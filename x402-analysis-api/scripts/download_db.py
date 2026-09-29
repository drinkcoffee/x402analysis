#!/usr/bin/env python3
"""Downloads the entire Neon database into a local Postgres database, so
update.py (and anyone poking around by hand) can read/write against a local
mirror instead of hitting Neon on every query.

The local database is a real, separate PostgreSQL server -- not the same
Postgres install/data any other project on this machine might use -- with
its own data directory, initialized (via `initdb`) the first time this is
needed and left running on its own port (5433 by default; override with
LOCAL_DB_PORT) so it doesn't collide with a system Postgres on the usual
5432. Everything it creates lives under scripts/../localdb/ (i.e.
x402-analysis-api/localdb/), which is gitignored (see the root
.gitignore's `/x402-analysis-api/localdb/` entry) -- this is local scratch
state, not something to commit.

Requires the `psql`/`pg_dump`/`initdb`/`pg_ctl`/`pg_isready` binaries from a
PostgreSQL installation (e.g. `brew install postgresql@16`) to be reachable
either on PATH or at one of a few common Homebrew install locations (see
_pg_bin) -- this script does not install PostgreSQL itself.

The download is a full, clean mirror each time it runs: the local database
is dropped and recreated before the dump is restored into it, so re-running
this (e.g. `python scripts/download_db.py` directly, to refresh a stale
local copy) never leaves stray objects behind from a previous download.
`pg_dump --no-owner --no-privileges` is used so the dump doesn't carry
GRANT/OWNER statements referencing Neon-specific roles that don't exist on
the local server (a well-known snag when restoring a managed Postgres dump
into a vanilla local one).

is_downloaded(neon_url) / ensure_server_running() / download() /
local_db_url() / preflight_checks() are meant to be imported by other
scripts (see update.py) as well as run directly here. is_downloaded() takes
the Neon URL currently in play and returns False if the local mirror was
actually downloaded from a *different* one -- see its docstring for why
that check exists. preflight_checks() reports any missing PostgreSQL
binary or port conflict up front, before ensure_server_running()/download()
would otherwise fail partway through.

Usage:
    python scripts/download_db.py

Requires DATABASE_URL (Neon), read from a .env file in the project root (if
present) or the real environment.
"""

from __future__ import annotations

import getpass
import hashlib
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

import preflight  # noqa: E402

try:
    from dotenv import load_dotenv
    import psycopg2
except ImportError as exc:
    print(
        f"missing required package: {exc.name}. Run `pip install -r "
        "requirements.txt` from x402-analysis-api/, with your virtualenv "
        "active.",
        file=sys.stderr,
    )
    sys.exit(1)

load_dotenv(PROJECT_ROOT / ".env")

import os  # noqa: E402

LOCAL_DB_ROOT = PROJECT_ROOT / "localdb"
LOCAL_DATA_DIR = LOCAL_DB_ROOT / "pgdata"
LOCAL_LOG_FILE = LOCAL_DB_ROOT / "postgres.log"
LOCAL_DOWNLOADED_MARKER = LOCAL_DB_ROOT / ".downloaded"

LOCAL_DB_PORT = int(os.getenv("LOCAL_DB_PORT", "5433"))
LOCAL_DB_NAME = "x402_local"

# A handful of common install locations for a Homebrew-installed PostgreSQL
# that isn't (only) symlinked onto PATH -- e.g. `brew install postgresql@16`
# without also doing `brew link`.
_COMMON_PG_BIN_DIRS = [
    f"/opt/homebrew/opt/postgresql@{v}/bin" for v in (18, 17, 16, 15, 14)
] + [f"/usr/local/opt/postgresql@{v}/bin" for v in (18, 17, 16, 15, 14)]


def _pg_bin(name: str) -> str:
    """Absolute path to a PostgreSQL client/server binary (initdb, pg_ctl,
    pg_isready, pg_dump, psql). Prefers the newest installed PostgreSQL
    version it can find under the common Homebrew locations (checked newest
    major version first) over whatever happens to be first on PATH, and
    only falls back to plain PATH lookup if none of those exist.

    This order matters specifically for pg_dump: it refuses to dump from a
    server *newer* than itself ("aborting because of server version
    mismatch"), and Neon stays current on major versions. If an older
    postgresql@16 happens to be the one linked onto PATH while a newer
    postgresql@18 is also installed but unlinked, checking PATH first would
    silently pick the incompatible one instead of the perfectly good newer
    one sitting right there -- so the versioned locations are checked
    first, newest to oldest, and PATH is only the last resort."""
    for bin_dir in _COMMON_PG_BIN_DIRS:
        candidate = Path(bin_dir) / name
        if candidate.exists():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(
        f"couldn't find the '{name}' PostgreSQL binary on PATH or in any of "
        f"{_COMMON_PG_BIN_DIRS}. Install a PostgreSQL version at least as "
        "new as your Neon database's (check its version with `SELECT "
        "version();`; e.g. `brew install postgresql@18`) or add its bin/ "
        "directory to PATH."
    )


def preflight_checks() -> list[str]:
    """Problems that would stop ensure_server_running()/download() from
    working: any of the PostgreSQL binaries they shell out to, and the
    local port they need free. Doesn't check DATABASE_URL -- that's the
    caller's Neon connection to check (preflight.require_database_url),
    not something this module owns."""
    problems: list[str] = []
    missing_binary = False

    for name in ("initdb", "pg_ctl", "pg_isready", "pg_dump", "psql"):
        try:
            _pg_bin(name)
        except RuntimeError as exc:
            problems.append(str(exc))
            missing_binary = True

    # _server_running() itself needs pg_ctl -- skip the port check rather
    # than raise a second time if that's already one of the problems above.
    if not missing_binary and not _server_running():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("localhost", LOCAL_DB_PORT)) == 0:
                problems.append(
                    f"port {LOCAL_DB_PORT} is already in use by something "
                    "else (not this project's local Postgres mirror, which "
                    "isn't running yet). Set LOCAL_DB_PORT to a different "
                    "port, or stop whatever's using it."
                )

    return problems


def local_db_url() -> str:
    """Connection string for the local mirror database."""
    return f"postgresql://{getpass.getuser()}@localhost:{LOCAL_DB_PORT}/{LOCAL_DB_NAME}"


def _local_maintenance_db_url() -> str:
    """Connection string for the local server's always-present `postgres`
    maintenance database -- used to create/drop LOCAL_DB_NAME itself, since
    you can't DROP/CREATE the database you're currently connected to."""
    return f"postgresql://{getpass.getuser()}@localhost:{LOCAL_DB_PORT}/postgres"


def _hash_url(url: str) -> str:
    # Not for secrecy -- just so the marker file (which sticks around on
    # disk) doesn't hold a Neon connection string, including its password,
    # in plaintext.
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def is_downloaded(neon_url: str) -> bool:
    """Whether download() has completed successfully for *this exact* Neon
    database. Also False if the local mirror was last downloaded from a
    DIFFERENT DATABASE_URL: comparing a local mirror of one Neon database
    against another is exactly how one mismatched test run ended up
    writing bad data to production (see update.py's module docstring) --
    this makes that pairing impossible to get wrong silently, by always
    forcing a fresh download instead. Doesn't guarantee the local server is
    currently *running* either way -- call ensure_server_running() for
    that regardless of this result."""
    if not LOCAL_DOWNLOADED_MARKER.exists():
        return False
    return LOCAL_DOWNLOADED_MARKER.read_text().strip() == _hash_url(neon_url)


def _data_dir_initialized() -> bool:
    return (LOCAL_DATA_DIR / "PG_VERSION").exists()


def _server_running() -> bool:
    result = subprocess.run(
        [_pg_bin("pg_ctl"), "-D", str(LOCAL_DATA_DIR), "status"],
        capture_output=True,
    )
    return result.returncode == 0


def _wait_until_ready(timeout: float = 15.0) -> None:
    pg_isready = _pg_bin("pg_isready")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if subprocess.run([pg_isready, "-p", str(LOCAL_DB_PORT)], capture_output=True).returncode == 0:
            return
        time.sleep(0.3)
    raise RuntimeError(f"local Postgres on port {LOCAL_DB_PORT} did not become ready within {timeout}s")


def ensure_server_running() -> None:
    """Initializes the local data directory if this is the very first run,
    then starts the local Postgres server if it isn't already running.
    Idempotent -- safe to call on every run of update.py, not just once."""
    LOCAL_DB_ROOT.mkdir(parents=True, exist_ok=True)

    if not _data_dir_initialized():
        print(f"initializing local Postgres data directory at {LOCAL_DATA_DIR} ...")
        subprocess.run(
            [
                _pg_bin("initdb"),
                "-D", str(LOCAL_DATA_DIR),
                "-U", getpass.getuser(),
                "--auth=trust",  # local-only dev database, never exposed off localhost
            ],
            check=True,
        )

    if not _server_running():
        print(f"starting local Postgres on port {LOCAL_DB_PORT} ...")
        subprocess.run(
            [
                _pg_bin("pg_ctl"),
                "-D", str(LOCAL_DATA_DIR),
                "-o", f"-p {LOCAL_DB_PORT}",
                "-l", str(LOCAL_LOG_FILE),
                "start",
            ],
            check=True,
        )
        _wait_until_ready()


def _recreate_local_database() -> None:
    """Drops and recreates LOCAL_DB_NAME so every download() starts from a
    clean slate -- connects to the `postgres` maintenance database, since a
    database can't drop/create itself."""
    conn = psycopg2.connect(_local_maintenance_db_url())
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{LOCAL_DB_NAME}"')
            cur.execute(f'CREATE DATABASE "{LOCAL_DB_NAME}"')
    finally:
        conn.close()


def _dump_and_restore(neon_url: str) -> None:
    print("dumping the Neon database...")
    dump_proc = subprocess.Popen(
        [_pg_bin("pg_dump"), "--no-owner", "--no-privileges", neon_url],
        stdout=subprocess.PIPE,
    )
    print("restoring into the local database...")
    restore = subprocess.run(
        [_pg_bin("psql"), "-q", local_db_url()],
        stdin=dump_proc.stdout,
        capture_output=True,
        text=True,
    )
    dump_proc.stdout.close()
    dump_exit = dump_proc.wait()
    if dump_exit != 0:
        raise RuntimeError(f"pg_dump exited with status {dump_exit}")
    if restore.returncode != 0:
        raise RuntimeError(f"psql restore failed (exit {restore.returncode}):\n{restore.stderr}")


def download(neon_url: Optional[str] = None) -> None:
    """Full download: ensure the local server is up, recreate the local
    database fresh, then dump Neon straight into it."""
    neon_url = neon_url or os.getenv("DATABASE_URL")
    if not neon_url:
        raise RuntimeError("DATABASE_URL is not set.")

    ensure_server_running()
    _recreate_local_database()
    _dump_and_restore(neon_url)

    LOCAL_DOWNLOADED_MARKER.write_text(_hash_url(neon_url))
    print(f"downloaded Neon database into the local database ({local_db_url()})")


def main() -> None:
    neon_url = os.getenv("DATABASE_URL")
    preflight.check_or_exit(
        preflight_checks,
        preflight.require_database_url(neon_url),
    )
    download(neon_url)


if __name__ == "__main__":
    main()
