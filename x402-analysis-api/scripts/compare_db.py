#!/usr/bin/env python3
"""Checks that the local Postgres mirror (localdb/) and Neon hold the same
data, and reports any differences. Read-only: changes neither database.

update.py and check_servers.py compute their changes once, against the
local mirror, and then replay them against local and then Neon as two
separate commits. If a Neon write fails partway through (or a run is
killed), the two can drift apart. This shows where.

Rows are matched by natural key, never by numeric id -- the two databases
assign their own ids independently, so ids aren't expected to match.
Foreign keys are compared by what they point at (a facilitator's api URL,
a service's server name, a link's owner name and address):

  table          matched by                        compared
  -------------  --------------------------------  ---------------------------
  facilitator    name                              api/doc/website/x402scan
                                                   URLs, risk, active, notes
  server         name                              same as facilitator
  service        server name + path                description, price, tags,
                                                   category, active, risk,
                                                   notes
  uris           url                               ip, location, subject,
                                                   risk, notes
  addresses      address                           chains, source, risk, notes
  linkaddresses  owner + owner's name + address    (presence only)
  api_keys       key hash                          access level, label
  clients        (count only -- no natural key)

`updated` dates are ignored by default (both databases set it to
CURRENT_DATE independently, so a run that straddles midnight makes them
differ without anything actually being wrong); --include-updated compares
them too.

Every query runs with a lock timeout: if update.py is partway through
writing to Neon, its transaction holds exclusive locks on several tables
(see ensure_schema), and a plain read would just hang until it commits.

Exit status: 0 if consistent, 1 if any difference was found, 2 if the
comparison couldn't be run.

Usage:
    python scripts/compare_db.py [--limit N] [--include-updated]

Requires DATABASE_URL (Neon), read from a .env file in the project root (if
present) or the real environment, and a local mirror downloaded from that
same DATABASE_URL (python scripts/download_db.py).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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

import download_db  # noqa: E402

LOCK_TIMEOUT = "10s"

# Each query returns the natural-key columns first (KEY_COLUMNS[table] of
# them), then the compared columns, then `updated` last.
_URI = "(SELECT url FROM uris WHERE id = {col})"

QUERIES: dict[str, tuple[int, list[str], str]] = {
    "facilitator": (
        1,
        ["name", "api", "doc", "website", "x402scan", "risk", "active", "notes", "updated"],
        f"""
        SELECT f.name, {_URI.format(col="f.api")}, {_URI.format(col="f.doc")},
               {_URI.format(col="f.website")}, {_URI.format(col="f.x402scan")},
               f.risk, f.active, f.notes, f.updated
        FROM facilitator f
        """,
    ),
    "server": (
        1,
        ["name", "api", "doc", "website", "x402scan", "risk", "active", "notes", "updated"],
        f"""
        SELECT s.name, {_URI.format(col="s.api")}, {_URI.format(col="s.doc")},
               {_URI.format(col="s.website")}, {_URI.format(col="s.x402scan")},
               s.risk, s.active, s.notes, s.updated
        FROM server s
        """,
    ),
    "service": (
        2,
        ["server", "path", "description", "price", "tags", "category", "active", "risk", "notes", "updated"],
        """
        SELECT s.name, sv.path, sv.description, sv.price, sv.tags, sv.category,
               sv.active, sv.risk, sv.notes, sv.updated
        FROM service sv
        JOIN server s ON s.id = sv.server
        """,
    ),
    "uris": (
        1,
        ["url", "ip", "location", "subject", "risk", "notes", "updated"],
        "SELECT url, ip, location, subject, risk, notes, updated FROM uris",
    ),
    "addresses": (
        1,
        ["address", "chains", "source", "risk", "notes", "updated"],
        "SELECT address, chains, source, risk, notes, updated FROM addresses",
    ),
    # owner 0/1 resolve to the facilitator/server name. Clients (2) have no
    # natural key, and funder/associate (3/4) have no table yet, so those
    # fall back to the raw ref id -- a mismatch there may just be differing
    # ids rather than a real difference.
    "linkaddresses": (
        3,
        ["owner", "owner name/ref", "address"],
        """
        SELECT la.owner,
               COALESCE(
                   CASE la.owner
                       WHEN 0 THEN (SELECT name FROM facilitator WHERE id = la.ref)
                       WHEN 1 THEN (SELECT name FROM server WHERE id = la.ref)
                   END,
                   '#' || la.ref
               ),
               a.address
        FROM linkaddresses la
        JOIN addresses a ON a.id = la.address
        """,
    ),
    "api_keys": (
        1,
        ["key_hash", "access_level", "label"],
        "SELECT key_hash, access_level, label FROM api_keys",
    ),
}


def fetch_table(cur, table: str, include_updated: bool) -> dict[tuple, tuple]:
    """natural key -> compared values, for every row of `table`."""
    key_count, columns, sql = QUERIES[table]
    cur.execute(sql)
    rows: dict[tuple, tuple] = {}
    has_updated = columns[-1] == "updated"
    for row in cur.fetchall():
        if has_updated and not include_updated:
            row = row[:-1]
        rows[tuple(row[:key_count])] = tuple(row[key_count:])
    return rows


def fetch_all(url: str, include_updated: bool) -> tuple[dict[str, dict], int]:
    """({table: rows}, client count) for one database, read in a single
    read-only transaction so the snapshot is consistent across tables."""
    conn = psycopg2.connect(url)
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = %s", (LOCK_TIMEOUT,))
            tables = {table: fetch_table(cur, table, include_updated) for table in QUERIES}
            cur.execute("SELECT count(*) FROM clients")
            client_count = cur.fetchone()[0]
        conn.rollback()
        return tables, client_count
    finally:
        conn.close()


def _fmt_key(key: tuple) -> str:
    return " / ".join(str(k) for k in key)


def compare_table(table: str, local: dict, neon: dict, limit: int) -> int:
    """Prints the differences for one table; returns how many there were."""
    key_count, columns, _sql = QUERIES[table]
    value_columns = columns[key_count:]

    only_local = sorted(set(local) - set(neon), key=str)
    only_neon = sorted(set(neon) - set(local), key=str)
    differing = sorted((k for k in set(local) & set(neon) if local[k] != neon[k]), key=str)
    total = len(only_local) + len(only_neon) + len(differing)

    status = "OK" if total == 0 else f"{total} difference(s)"
    print(f"{table:<14} local {len(local):>7}  neon {len(neon):>7}  {status}")
    if total == 0:
        return 0

    def _section(label: str, keys: list) -> None:
        if not keys:
            return
        print(f"    {label}: {len(keys)}")
        for key in keys[:limit]:
            print(f"      {_fmt_key(key)}")
        if len(keys) > limit:
            print(f"      ... and {len(keys) - limit} more")

    _section("only in local", only_local)
    _section("only in neon", only_neon)
    if differing:
        print(f"    different values: {len(differing)}")
        for key in differing[:limit]:
            changes = [
                f"{col}: local={lv!r} neon={nv!r}"
                for col, lv, nv in zip(value_columns, local[key], neon[key])
                if lv != nv
            ]
            print(f"      {_fmt_key(key)}: " + "; ".join(changes))
        if len(differing) > limit:
            print(f"      ... and {len(differing) - limit} more")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=10, help="examples to print per kind of difference (default 10)")
    parser.add_argument("--include-updated", action="store_true", help="also compare `updated` dates")
    args = parser.parse_args()

    neon_url = os.getenv("DATABASE_URL")
    preflight.check_or_exit(
        preflight.require_database_url(neon_url),
        download_db.preflight_checks,
    )

    # Deliberately never downloads: a fresh download would trivially match,
    # which defeats the point of checking.
    if not download_db.is_downloaded(neon_url):
        print(
            "the local database hasn't been downloaded from this DATABASE_URL, so there's "
            "nothing meaningful to compare -- run `python scripts/download_db.py` first.",
            file=sys.stderr,
        )
        sys.exit(2)
    download_db.ensure_server_running()

    try:
        print("reading local database ...")
        local_tables, local_clients = fetch_all(download_db.local_db_url(), args.include_updated)
        print("reading Neon ...")
        neon_tables, neon_clients = fetch_all(neon_url, args.include_updated)
    except psycopg2.errors.LockNotAvailable:
        print(
            f"timed out after {LOCK_TIMEOUT} waiting for a table lock -- something (most likely "
            "update.py or check_servers.py) is partway through writing. Try again once it's done.",
            file=sys.stderr,
        )
        sys.exit(2)
    print()

    differences = 0
    for table in QUERIES:
        differences += compare_table(table, local_tables[table], neon_tables[table], args.limit)
    client_status = "OK" if local_clients == neon_clients else "count differs"
    print(f"{'clients':<14} local {local_clients:>7}  neon {neon_clients:>7}  {client_status}")
    differences += local_clients != neon_clients

    print()
    if differences:
        print(f"INCONSISTENT: {differences} difference(s) found")
        sys.exit(1)
    print("consistent")


if __name__ == "__main__":
    main()
