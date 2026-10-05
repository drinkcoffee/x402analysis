#!/usr/bin/env python3
"""Geolocates every server whose api URL has no location recorded, and
writes the result to both the local Postgres mirror (localdb/) and Neon.

Steps:
  1. Find every server in the local mirror with an api URL whose uris row
     has no `location`.
  2. Fingerprint those URLs again with load_facilitators.fingerprint_urls
     -- the same DNS lookup, TLS handshake and ip-api.com geolocation used
     when URLs are first added.
  3. For every URL that now has a location, update its uris row's ip and
     location (and its TLS certificate subject, if one was found) in the
     local mirror, then in Neon, matched by url. URLs that still can't be
     geolocated (e.g. the hostname no longer resolves) are listed and left
     alone.

Neon is updated by url, so a URL that's in the local mirror but not (yet)
in Neon is skipped there -- the output says how many rows each database
actually updated. scripts/push_to_neon.py brings Neon up to date with the
mirror if that happens.

Refuses to run unless the local mirror was downloaded from this exact
DATABASE_URL (download_db.is_downloaded), the same safeguard update.py
uses, so changes are never written to a Neon database the mirror doesn't
reflect.

Usage:
    python scripts/fix_location.py

Requires DATABASE_URL (Neon), read from a .env file in the project root (if
present) or the real environment, and the sibling cli-tool project checked
out next to this one (for fingerprint_urls).
"""

from __future__ import annotations

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
from load_facilitators import fingerprint_urls  # noqa: E402

LOCK_TIMEOUT = "10s"


def fetch_unlocated_api_urls(cur) -> list[tuple[str, str]]:
    """(server name, api url) for every server whose api URL has no
    location."""
    cur.execute(
        """
        SELECT s.name, u.url
        FROM server s
        JOIN uris u ON u.id = s.api
        WHERE u.location IS NULL
        ORDER BY s.name
        """
    )
    return cur.fetchall()


def apply_locations(cur, fingerprints: dict[str, dict]) -> int:
    """Writes each fingerprint's ip/location/subject to its uris row (by
    url); returns how many rows were updated. A subject that couldn't be
    read this time doesn't overwrite one already stored."""
    updated = 0
    for url, fp in fingerprints.items():
        cur.execute(
            """
            UPDATE uris
            SET ip = %s, location = %s, subject = COALESCE(%s, subject), updated = CURRENT_DATE
            WHERE url = %s
            """,
            (fp["ip"], fp["location"], fp["subject"], url),
        )
        updated += cur.rowcount
    return updated


def main() -> None:
    neon_url = os.getenv("DATABASE_URL")
    preflight.check_or_exit(
        preflight.require_database_url(neon_url),
        download_db.preflight_checks,
    )
    if not download_db.is_downloaded(neon_url):
        print(
            "refusing to run: the local database wasn't downloaded from this DATABASE_URL -- "
            "run `python scripts/download_db.py` first.",
            file=sys.stderr,
        )
        sys.exit(2)
    download_db.ensure_server_running()

    conn = psycopg2.connect(download_db.local_db_url())
    try:
        with conn.cursor() as cur:
            servers = fetch_unlocated_api_urls(cur)
    finally:
        conn.close()

    if not servers:
        print("every server with an api URL already has a location")
        return
    print(f"{len(servers)} server(s) with an api URL but no location")

    # No database connection is held open while this runs -- it's slow
    # network work, and Neon drops connections left idle too long.
    fingerprints = fingerprint_urls({url for _name, url in servers})
    located = {url: fp for url, fp in fingerprints.items() if fp["location"]}

    for name, url in servers:
        location = located.get(url, {}).get("location")
        print(f"  {name}: {url} -> {location or 'still not geolocated'}")

    if not located:
        print("nothing could be geolocated -- no changes made")
        return

    local_conn = psycopg2.connect(download_db.local_db_url())
    neon_conn = psycopg2.connect(neon_url)
    counts = {}
    try:
        for label, conn in (("local", local_conn), ("Neon", neon_conn)):
            with conn.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = %s", (LOCK_TIMEOUT,))
                counts[label] = apply_locations(cur, located)
            conn.commit()
    except psycopg2.errors.LockNotAvailable:
        done = " (local was already updated -- `python scripts/push_to_neon.py` will copy it to Neon)" if counts else ""
        print(
            f"timed out after {LOCK_TIMEOUT} waiting for a table lock -- something else (most likely "
            f"update.py) is writing. Try again once it's done{done}.",
            file=sys.stderr,
        )
        sys.exit(2)
    finally:
        local_conn.close()
        neon_conn.close()

    print(
        f"geolocated {len(located)} of {len(servers)} URL(s); "
        f"updated {counts['local']} row(s) locally and {counts['Neon']} in Neon"
    )


if __name__ == "__main__":
    main()
