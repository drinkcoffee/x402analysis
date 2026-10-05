#!/usr/bin/env python3
"""Reports where facilitators and servers are hosted, from the local
Postgres mirror (localdb/). Read-only.

A facilitator's/server's location is its api URL's geolocation (the
`location` column of its uris row: "City, Region, Country", from
ip-api.com -- see load_facilitators.fingerprint_urls). Facilitators are
reported first, then servers. Within each, locations are grouped by city
and listed from most to least used, in two sub-sections -- USA and Canada,
then the rest of the world -- followed by how many haven't been
geolocated (no api URL at all, or an api URL with no location recorded).

Usage:
    python scripts/l_location.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

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

import download_db  # noqa: E402
import preflight  # noqa: E402

NORTH_AMERICA = {"United States", "Canada"}


def fetch_locations(cur, table: str) -> list[tuple[str | None, str | None]]:
    """(api url, location) for every row of `table` (facilitator or
    server); either can be None."""
    cur.execute(
        f"""
        SELECT u.url, u.location
        FROM {table} t
        LEFT JOIN uris u ON u.id = t.api
        """
    )
    return cur.fetchall()


def _country(location: str) -> str:
    return location.rsplit(",", 1)[-1].strip()


def _print_counts(title: str, counts: Counter) -> None:
    total = sum(counts.values())
    print(f"  {title}: {total} in {len(counts)} location(s)")
    if not counts:
        print("    (none)")
    width = len(str(max(counts.values(), default=0)))
    for location, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        print(f"    {count:>{width}}  {location}")


def report(label: str, rows: list[tuple[str | None, str | None]]) -> None:
    north_america: Counter = Counter()
    rest_of_world: Counter = Counter()
    no_api = 0
    not_geolocated = 0
    for url, location in rows:
        if not url:
            no_api += 1
        elif not location:
            not_geolocated += 1
        elif _country(location) in NORTH_AMERICA:
            north_america[location] += 1
        else:
            rest_of_world[location] += 1

    print(f"{label.upper()} ({len(rows)} total)")
    print("=" * (len(label) + len(str(len(rows))) + 9))
    _print_counts("USA and Canada", north_america)
    print()
    _print_counts("Rest of the world", rest_of_world)
    print()
    print(f"  Not geolocated: {no_api + not_geolocated}")
    print(f"    {no_api} with no api URL")
    print(f"    {not_geolocated} with an api URL but no location")
    print()


def main() -> None:
    preflight.check_or_exit(download_db.preflight_checks)
    if not download_db.LOCAL_DOWNLOADED_MARKER.exists():
        print(
            "the local database hasn't been downloaded yet -- run `python scripts/download_db.py` first.",
            file=sys.stderr,
        )
        sys.exit(2)
    download_db.ensure_server_running()

    conn = psycopg2.connect(download_db.local_db_url())
    try:
        with conn.cursor() as cur:
            facilitators = fetch_locations(cur, "facilitator")
            servers = fetch_locations(cur, "server")
    finally:
        conn.close()

    report("Facilitators", facilitators)
    report("Servers", servers)


if __name__ == "__main__":
    main()
