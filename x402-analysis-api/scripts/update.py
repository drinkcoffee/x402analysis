#!/usr/bin/env python3
"""Refreshes facilitator data from x402scan.com and re-checks which
facilitators are actually live, writing any changes to both the local
Postgres mirror and Neon.

Steps:
  1. Make sure the local database exists (download_db.download(), if it
     hasn't been downloaded yet) and its server is running.
  2. Scrape x402scan.com's Facilitators page (x402scan_scraper.py, vendored
     from the sibling cli-tool project -- see that file's docstring).
  3. Compare the scrape against the local database:
       - a facilitator x402scan knows about that the database doesn't ->
         a new facilitator row (api URL left unset -- x402scan's data
         doesn't include one, only a doc URL and its own x402scan.com page
         URL; see x402scan_scraper.py's docstring for why).
       - a doc/x402scan URL that's different from what's stored -> updated.
       - an address x402scan lists that isn't already linked to that
         facilitator -> added (never removed -- x402scan not re-listing an
         address isn't treated as evidence it's no longer valid).
  4. Re-check every facilitator's liveness the same way check_supported.py
     does (imported from it directly, not shelled out to, so this reuses
     its exact CDP-authentication and JSON-validity logic without
     re-parsing printed output): a facilitator with no api URL is not
     active; one with an api URL whose /supported response isn't valid
     JSON is not active; anything else is active. Wherever that disagrees
     with the stored `active` value, it's updated.
  5. Every change from steps 3-4 is applied to *both* the local database
     and Neon -- computed once (against the local mirror, which is assumed
     to already match Neon, since this script is the only thing that's
     supposed to write to either), then replayed identically against each
     connection so the two never drift apart. Each connection resolves its
     own foreign keys via upsert-by-natural-key (url/address/name), so the
     two databases' own internal row ids never need to match.

This isn't a distributed transaction: the local commit and the Neon commit
are two separate commits, in that order. If the Neon commit fails after the
local one succeeds, the two databases are left temporarily out of sync
until the next run (which would just re-derive and re-apply the same
still-pending changes to Neon) -- acceptable for a periodically-run
maintenance script, not something to build real two-phase-commit machinery
around.

Step 3's diff is only ever safe to apply to Neon if the local mirror it was
computed against actually reflects *that* Neon database. If DATABASE_URL
ever points somewhere else than whatever the local mirror was last
downloaded from -- a different Neon project/branch, or a stale local mirror
left over from testing against a throwaway database -- every facilitator
that Neon already has but the (wrong) local mirror doesn't looks "new,"
and applying that diff to Neon overwrites those facilitators' real api/doc/
x402scan with whatever the scrape/local-mirror side had (nothing, for api).
That's not hypothetical: it's exactly what happened once during this
script's own development, wiping the api URL (and flipping `active` to
false) for nine real facilitators in production before it was caught and
fixed by hand. download_db.is_downloaded(neon_url) now guards against
exactly this -- it checks the local mirror was downloaded from this exact
DATABASE_URL (by comparing against a hash recorded at download time, not
just "some download happened at some point"), and forces a fresh download
whenever that doesn't match, rather than silently diffing against
whatever's on disk.

Before any of the above: every required env var, library, and external
tool is checked up front and reported together (preflight.py), rather than
failing with a traceback -- or worse, a partially-applied change -- partway
through a run:
  - required packages (psycopg2, requests, python-dotenv, pyjwt,
    cryptography) via a guarded import at the top of this file and of each
    module it imports (check_supported.py, download_db.py,
    load_facilitators.py) -- missing one prints which one and exits
    immediately, since nothing downstream can work without it anyway.
  - the sibling cli-tool project checked out next to this one (needed by
    load_facilitators.fingerprint_urls, transitively) -- same treatment,
    checked where that import happens.
  - DATABASE_URL (Neon): set, and actually reachable.
  - CDP_API_KEY_ID/CDP_API_KEY_SECRET (check_supported.preflight_checks()):
    needed for step 4 to check Coinbase specifically.
  - the PostgreSQL binaries download_db.py shells out to, and its local
    port being free (download_db.preflight_checks()).

Usage:
    python scripts/update.py

Requires DATABASE_URL (Neon) and CDP_API_KEY_ID/CDP_API_KEY_SECRET, read
from a .env file in the project root (if present) or the real environment.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

import preflight  # noqa: E402

try:
    from dotenv import load_dotenv
    import psycopg2
    import psycopg2.extras
    import requests  # needed by x402scan_scraper, imported below
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

# Each of these has its own guarded import for its own extra dependencies
# (requests, pyjwt/cryptography via cdp_client, cli-tool's x402tool via
# load_facilitators) -- if any of those are missing, importing it here
# prints a clear message and exits immediately, the same way a missing
# package above does.
import check_supported  # noqa: E402
import download_db  # noqa: E402
import x402scan_scraper  # noqa: E402
from db_setup import ensure_schema  # noqa: E402
from load_facilitators import _link_address, _upsert_facilitator, _upsert_uri, fingerprint_urls  # noqa: E402

OWNER_FACILITATOR = 0


def _normalize_address(address: str) -> str:
    """Case-insensitive for 0x-prefixed (EVM) addresses (case isn't
    significant for those); case-sensitive otherwise, e.g. Solana base58
    addresses -- same rule cli-tool's combine_facilitators.py uses, so an
    address already in the database under different EVM-address casing
    isn't reported as "new"."""
    return address.lower() if address.lower().startswith("0x") else address


def fetch_local_facilitators(cur) -> dict[str, dict]:
    """name.lower() -> {"id", "name", "api", "active", "api_url", "doc_url",
    "x402scan_url"} for every facilitator currently in the database."""
    cur.execute(
        """
        SELECT f.id, f.name, f.api, f.active,
               api_uri.url AS api_url,
               doc_uri.url AS doc_url,
               x402scan_uri.url AS x402scan_url
        FROM facilitator f
        LEFT JOIN uris api_uri ON api_uri.id = f.api
        LEFT JOIN uris doc_uri ON doc_uri.id = f.doc
        LEFT JOIN uris x402scan_uri ON x402scan_uri.id = f.x402scan
        """
    )
    return {row["name"].lower(): dict(row) for row in cur.fetchall()}


def fetch_local_addresses(cur) -> dict[int, set[str]]:
    """facilitator id -> the set of addresses currently linked to it."""
    cur.execute(
        """
        SELECT la.ref AS facilitator_id, a.address
        FROM linkaddresses la
        JOIN addresses a ON a.id = la.address
        WHERE la.owner = %s
        """,
        (OWNER_FACILITATOR,),
    )
    result: dict[int, set[str]] = {}
    for row in cur.fetchall():
        result.setdefault(row["facilitator_id"], set()).add(row["address"])
    return result


def build_ops(
    scraped: list[dict],
    local_facilitators: dict[str, dict],
    local_addresses: dict[int, set[str]],
) -> list[dict]:
    """Diffs x402scan's scrape against the local database and returns the
    list of changes needed to bring both databases up to date with it --
    doesn't touch either database itself (see apply_ops)."""
    ops: list[dict] = []

    for item in scraped:
        name = item.get("name")
        if not name:
            continue
        if item.get("errors"):
            print(f"  note: x402scan scrape for {name!r} had issues: {'; '.join(item['errors'])}")

        existing = local_facilitators.get(name.lower())
        doc_url = item.get("docs_url")
        x402scan_url = item.get("url")
        addresses = item.get("addresses") or []

        if existing is None:
            ops.append(
                {
                    "type": "new_facilitator",
                    "name": name,
                    "doc_url": doc_url,
                    "x402scan_url": x402scan_url,
                    "addresses": list(addresses),
                }
            )
            continue

        if doc_url and doc_url != existing["doc_url"]:
            ops.append({"type": "update_url", "name": existing["name"], "field": "doc", "url": doc_url})
        if x402scan_url and x402scan_url != existing["x402scan_url"]:
            ops.append({"type": "update_url", "name": existing["name"], "field": "x402scan", "url": x402scan_url})

        known = {_normalize_address(a) for a in local_addresses.get(existing["id"], set())}
        for address in addresses:
            if _normalize_address(address) not in known:
                ops.append({"type": "new_address", "name": existing["name"], "address": address})

    return ops


def _get_or_create_address(cur, address: str) -> int:
    """Returns the id of `address`'s row in `addresses`, inserting a new
    one (chains/source unknown, source left as "x402scan") if it doesn't
    exist yet. Deliberately does NOT overwrite an existing row's
    chains/source the way load_facilitators.py's loader does -- that
    loader is refreshing from an authoritative source file and is meant to
    win; this is just noticing a previously-unknown address and shouldn't
    clobber richer data (e.g. a real chain list) some other source already
    recorded for it."""
    cur.execute("SELECT id FROM addresses WHERE address = %s", (address,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO addresses (address, chains, source) VALUES (%s, NULL, %s) RETURNING id",
        (address, "x402scan"),
    )
    return cur.fetchone()[0]


_URL_FIELD_COLUMNS = {"doc": "doc", "x402scan": "x402scan"}


def apply_ops(cur, ops: list[dict], fingerprints: dict[str, dict]) -> None:
    """Replays `ops` (from build_ops) against one database connection's
    cursor. Every op resolves facilitators/addresses/uris by their natural
    key (name/address/url) rather than a numeric id, so this can be called
    once per database (local, then Neon) without the two ever needing to
    agree on row ids."""
    for op in ops:
        if op["type"] == "new_facilitator":
            doc_id = _upsert_uri(cur, op["doc_url"], fingerprints) if op["doc_url"] else None
            x402scan_id = _upsert_uri(cur, op["x402scan_url"], fingerprints) if op["x402scan_url"] else None
            # api is left None: x402scan doesn't report one, and a
            # facilitator with no known api URL is inactive by definition
            # (see compute_active_ops) -- no separate insert needed to make
            # that true.
            facilitator_id = _upsert_facilitator(cur, op["name"], None, doc_id, x402scan_id)
            for address in op["addresses"]:
                address_id = _get_or_create_address(cur, address)
                _link_address(cur, OWNER_FACILITATOR, facilitator_id, address_id)

        elif op["type"] == "update_url":
            uri_id = _upsert_uri(cur, op["url"], fingerprints)
            column = _URL_FIELD_COLUMNS[op["field"]]
            cur.execute(
                f"UPDATE facilitator SET {column} = %s, updated = CURRENT_DATE WHERE name = %s",
                (uri_id, op["name"]),
            )

        elif op["type"] == "new_address":
            address_id = _get_or_create_address(cur, op["address"])
            cur.execute("SELECT id FROM facilitator WHERE name = %s", (op["name"],))
            facilitator_id = cur.fetchone()[0]
            _link_address(cur, OWNER_FACILITATOR, facilitator_id, address_id)

        elif op["type"] == "update_active":
            cur.execute(
                "UPDATE facilitator SET active = %s, updated = CURRENT_DATE WHERE name = %s",
                (op["active"], op["name"]),
            )

        else:
            raise ValueError(f"unknown op type: {op['type']!r}")


def compute_active_ops(cur) -> list[dict]:
    """Re-derives every facilitator's active state (no api URL, or an api
    URL whose /supported doesn't return valid JSON -> inactive; otherwise
    active) via check_supported.check_supported() -- the same function
    check_supported.py itself calls -- and returns an "update_active" op
    for each one that disagrees with what's currently stored."""
    cur.execute(
        """
        SELECT f.name, f.active, api_uri.url AS api_url
        FROM facilitator f
        LEFT JOIN uris api_uri ON api_uri.id = f.api
        """
    )
    rows = cur.fetchall()

    ops: list[dict] = []
    for row in rows:
        name, current_active, api_url = row["name"], row["active"], row["api_url"]
        if not api_url:
            desired_active = False
        else:
            _url_called, is_valid_json, _text = check_supported.check_supported(api_url)
            desired_active = is_valid_json
        if desired_active != current_active:
            ops.append({"type": "update_active", "name": name, "active": desired_active})
    return ops


def _fingerprint_targets(ops: list[dict]) -> set[str]:
    urls: set[str] = set()
    for op in ops:
        if op["type"] == "new_facilitator":
            urls.update(u for u in (op["doc_url"], op["x402scan_url"]) if u)
        elif op["type"] == "update_url":
            urls.add(op["url"])
    return urls


def main() -> None:
    neon_url = os.getenv("DATABASE_URL")
    preflight.check_or_exit(
        preflight.require_database_url(neon_url),
        check_supported.preflight_checks,
        download_db.preflight_checks,
    )

    if not download_db.is_downloaded(neon_url):
        print("local database hasn't been downloaded yet (or was downloaded from a different DATABASE_URL) -- downloading now...")
        download_db.download(neon_url)
    else:
        download_db.ensure_server_running()

    print("scraping x402scan.com/facilitators ...")
    scraped = x402scan_scraper.scrape_all_facilitators()
    print(f"scraped {len(scraped)} facilitator(s) from x402scan")

    local_conn = psycopg2.connect(download_db.local_db_url())
    neon_conn = psycopg2.connect(neon_url)
    try:
        for conn in (local_conn, neon_conn):
            with conn.cursor() as cur:
                ensure_schema(cur)
            conn.commit()

        with local_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            local_facilitators = fetch_local_facilitators(cur)
            local_addresses = fetch_local_addresses(cur)

        ops = build_ops(scraped, local_facilitators, local_addresses)
        fingerprints = fingerprint_urls(_fingerprint_targets(ops))

        for conn in (local_conn, neon_conn):
            with conn.cursor() as cur:
                apply_ops(cur, ops, fingerprints)
            conn.commit()

        with local_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            active_ops = compute_active_ops(cur)

        for conn in (local_conn, neon_conn):
            with conn.cursor() as cur:
                apply_ops(cur, active_ops, fingerprints={})
            conn.commit()
    finally:
        local_conn.close()
        neon_conn.close()

    new_facilitators = sum(1 for op in ops if op["type"] == "new_facilitator")
    new_addresses = sum(len(op["addresses"]) for op in ops if op["type"] == "new_facilitator") + sum(
        1 for op in ops if op["type"] == "new_address"
    )
    url_updates = sum(1 for op in ops if op["type"] == "update_url")

    print(
        f"{new_facilitators} new facilitator(s), {new_addresses} new address(es), "
        f"{url_updates} URL update(s), {len(active_ops)} active-state change(s)"
    )


if __name__ == "__main__":
    main()
