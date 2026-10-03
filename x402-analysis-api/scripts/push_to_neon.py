#!/usr/bin/env python3
"""Pushes the local Postgres mirror's (localdb/) data into Neon, making
Neon match it: every row that's missing from Neon is inserted, and every
row whose values differ is updated to the local values.

For recovering from a run of update.py/check_servers.py whose local commit
succeeded but whose Neon commit didn't (see update.py's docstring) -- in
that state, re-running update.py won't fix Neon, because it works out
what's "new" by comparing against the local mirror, which already has
everything. Run scripts/compare_db.py first to see what's different.

The local mirror is treated as the source of truth for:

  uris, addresses, facilitator, server, service, linkaddresses

Rows are matched by natural key (url, address, name, server name + path,
owner + owner name + address) -- never by numeric id, since the two
databases assign their own ids independently. Every foreign key is
re-resolved against Neon's own ids before writing. `updated` dates are
copied too, so after a push the two compare identical (compare_db.py
--include-updated).

Not touched:
  - rows that exist only in Neon: reported, never deleted.
  - api_keys: created directly against Neon (create_api_key.py), so Neon
    is the authority for those, not the mirror.
  - clients, and links to clients/funders/associates (linkaddresses owner
    2-4): no natural key to match on, so they can't be pushed safely.
    Reported if any differ.

Everything is written in one Neon transaction, in batches (one round trip
per few hundred rows, not one per row), so it's all-or-nothing and fast.
The schema is brought up to date first (ensure_schema) in its own short
transaction, so its ALTER TABLE locks aren't held during the push. A lock
timeout stops it waiting forever behind another script's open transaction.

Refuses to run unless the local mirror was downloaded from this exact
DATABASE_URL (download_db.is_downloaded) -- pushing a mirror of some
*other* database would overwrite good data in this one (see update.py's
docstring for the time that happened).

Usage:
    python scripts/push_to_neon.py --dry-run    # show what would be pushed
    python scripts/push_to_neon.py

Requires DATABASE_URL (Neon), read from a .env file in the project root (if
present) or the real environment.
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
    import psycopg2.extras
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

import compare_db  # noqa: E402
import download_db  # noqa: E402
from db_setup import ensure_schema  # noqa: E402

# In dependency order: every table's foreign keys point at tables earlier
# in this list.
PUSHED_TABLES = ["uris", "addresses", "facilitator", "server", "service", "linkaddresses"]
PAGE_SIZE = 500
LOCK_TIMEOUT = "10s"
OWNER_FACILITATOR = 0
OWNER_SERVER = 1


def rows_to_push(local: dict[tuple, tuple], neon: dict[tuple, tuple]) -> list[tuple]:
    """Every local row (key + values, as one tuple) that's missing from
    Neon or differs from Neon's version."""
    return [key + values for key, values in local.items() if neon.get(key) != values]


def _id_map(cur, sql: str) -> dict:
    cur.execute(sql)
    return {row[0]: row[1] for row in cur.fetchall()}


def _upsert(cur, sql: str, rows: list[tuple]) -> None:
    if rows:
        psycopg2.extras.execute_values(cur, sql, rows, page_size=PAGE_SIZE)


def push(cur, pending: dict[str, list[tuple]]) -> list[str]:
    """Writes every pending row to Neon through `cur`. Each row is in
    compare_db.QUERIES's column order: natural key first, then values,
    then `updated` (for tables that have it). Returns a description of
    anything that couldn't be pushed."""
    skipped: list[str] = []

    _upsert(
        cur,
        """
        INSERT INTO uris (url, ip, location, subject, risk, notes, updated) VALUES %s
        ON CONFLICT (url) DO UPDATE SET
            ip = EXCLUDED.ip, location = EXCLUDED.location, subject = EXCLUDED.subject,
            risk = EXCLUDED.risk, notes = EXCLUDED.notes, updated = EXCLUDED.updated
        """,
        pending["uris"],
    )
    _upsert(
        cur,
        """
        INSERT INTO addresses (address, chains, source, risk, notes, updated) VALUES %s
        ON CONFLICT (address) DO UPDATE SET
            chains = EXCLUDED.chains, source = EXCLUDED.source, risk = EXCLUDED.risk,
            notes = EXCLUDED.notes, updated = EXCLUDED.updated
        """,
        pending["addresses"],
    )

    # Every url a facilitator/server references is in Neon by now: either
    # it was already identical there, or it was just pushed above.
    uri_ids = _id_map(cur, "SELECT url, id FROM uris")

    def _with_uri_ids(row: tuple) -> tuple:
        name, api, doc, website, x402scan, *rest = row
        return (name, *(uri_ids[u] if u else None for u in (api, doc, website, x402scan)), *rest)

    for table in ("facilitator", "server"):
        _upsert(
            cur,
            f"""
            INSERT INTO {table} (name, api, doc, website, x402scan, risk, active, notes, updated) VALUES %s
            ON CONFLICT (name) DO UPDATE SET
                api = EXCLUDED.api, doc = EXCLUDED.doc, website = EXCLUDED.website,
                x402scan = EXCLUDED.x402scan, risk = EXCLUDED.risk, active = EXCLUDED.active,
                notes = EXCLUDED.notes, updated = EXCLUDED.updated
            """,
            [_with_uri_ids(row) for row in pending[table]],
        )

    server_ids = _id_map(cur, "SELECT name, id FROM server")
    _upsert(
        cur,
        """
        INSERT INTO service (server, path, description, price, tags, category, active, risk, notes, updated)
        VALUES %s
        ON CONFLICT (server, path) DO UPDATE SET
            description = EXCLUDED.description, price = EXCLUDED.price, tags = EXCLUDED.tags,
            category = EXCLUDED.category, active = EXCLUDED.active, risk = EXCLUDED.risk,
            notes = EXCLUDED.notes, updated = EXCLUDED.updated
        """,
        [(server_ids[server], *rest) for server, *rest in pending["service"]],
    )

    owner_ids = {
        OWNER_FACILITATOR: _id_map(cur, "SELECT name, id FROM facilitator"),
        OWNER_SERVER: server_ids,
    }
    address_ids = _id_map(cur, "SELECT address, id FROM addresses")
    links = []
    for owner, owner_name, address in pending["linkaddresses"]:
        if owner not in owner_ids:
            skipped.append(f"linkaddresses: owner {owner} {owner_name} -> {address} (no natural key)")
            continue
        links.append((owner, owner_ids[owner][owner_name], address_ids[address]))
    _upsert(
        cur,
        "INSERT INTO linkaddresses (owner, ref, address) VALUES %s ON CONFLICT DO NOTHING",
        links,
    )
    return skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report what would be pushed, without writing")
    args = parser.parse_args()

    neon_url = os.getenv("DATABASE_URL")
    preflight.check_or_exit(
        preflight.require_database_url(neon_url),
        download_db.preflight_checks,
    )
    if not download_db.is_downloaded(neon_url):
        print(
            "refusing to push: the local database wasn't downloaded from this DATABASE_URL, so "
            "it isn't a mirror of this Neon database.",
            file=sys.stderr,
        )
        sys.exit(2)
    download_db.ensure_server_running()

    try:
        print("reading local database ...")
        local_tables, local_clients = compare_db.fetch_all(download_db.local_db_url(), include_updated=True)
        print("reading Neon ...")
        neon_tables, neon_clients = compare_db.fetch_all(neon_url, include_updated=True)
    except psycopg2.errors.LockNotAvailable:
        print(
            f"timed out after {compare_db.LOCK_TIMEOUT} waiting for a table lock -- something (most "
            "likely update.py or check_servers.py) is partway through writing. Try again once it's done.",
            file=sys.stderr,
        )
        sys.exit(2)

    pending = {t: rows_to_push(local_tables[t], neon_tables[t]) for t in PUSHED_TABLES}
    print()
    for table in PUSHED_TABLES:
        local, neon = local_tables[table], neon_tables[table]
        new = sum(1 for key in local if key not in neon)
        changed = len(pending[table]) - new
        only_neon = sum(1 for key in neon if key not in local)
        note = f", {only_neon} only in Neon (left alone)" if only_neon else ""
        print(f"{table:<14} {new:>7} to insert, {changed:>6} to update{note}")
    if local_clients != neon_clients:
        print(f"clients: local has {local_clients}, Neon has {neon_clients} -- not pushed (no natural key)")
    if local_tables["api_keys"] != neon_tables["api_keys"]:
        print("api_keys differ -- not pushed (Neon is the authority for API keys)")

    total = sum(len(rows) for rows in pending.values())
    if total == 0:
        print("\nnothing to push")
        return
    if args.dry_run:
        print(f"\ndry run: {total} row(s) would be pushed")
        return

    conn = psycopg2.connect(neon_url)
    try:
        with conn.cursor() as cur:
            ensure_schema(cur)
        conn.commit()

        print(f"\npushing {total} row(s) to Neon ...")
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = %s", (LOCK_TIMEOUT,))
            skipped = push(cur, pending)
        conn.commit()
    except psycopg2.errors.LockNotAvailable:
        conn.rollback()
        print(
            f"timed out after {LOCK_TIMEOUT} waiting for a table lock -- nothing was pushed. Try "
            "again once whatever else is writing to Neon is done.",
            file=sys.stderr,
        )
        sys.exit(2)
    finally:
        conn.close()

    for line in skipped:
        print(f"  skipped {line}")
    print("done -- run `python scripts/compare_db.py` to confirm the two now match")


if __name__ == "__main__":
    main()
