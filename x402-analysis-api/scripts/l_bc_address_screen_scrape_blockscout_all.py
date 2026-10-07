#!/usr/bin/env python3
"""Prints the tags Blockscout's Ethereum explorer shows for every
Ethereum-style address (0x + 40 hex digits) in the local Postgres mirror's
(localdb/) addresses table, to the screen and to a CSV file. Read-only.

Tags come from Blockscout's public metadata service, the same source the
explorer's address page uses (see l_bc_address_screen_scrape_blockscout.py).
Every address is looked up on Ethereum mainnet, whatever chain it's
recorded as being on. Addresses are looked up 50 per request, with a
second between requests; a "." is printed to standard error per request.

One line per address, with columns:
    address, name, second name, tag, second tag, note
  - name: the address's name tags, as the page orders them; a third or
    later name is joined into "second name" ("; "-separated)
  - tag: every other tag (generic, protocol, ...), likewise -- extras are
    joined into "second tag"
  - note: the warning banner note(s), "; "-separated
An address with no tags gets a line with those columns blank. Addresses
that aren't Ethereum-style are skipped.

The screen has the columns padded with spaces; the CSV doesn't, and has a
header row.

Usage:
    python scripts/l_bc_address_screen_scrape_blockscout_all.py <csv file>
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    import psycopg2
    import requests
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
from l_bc_address_screen_scrape_blockscout import (  # noqa: E402
    ETH_ADDRESS,
    ETHEREUM_CHAIN_ID,
    METADATA_URL,
    TIMEOUT,
    _meta,
)

BATCH_SIZE = 50
REQUEST_INTERVAL = 1.0
COLUMNS = ["address", "name", "second name", "tag", "second tag", "note"]


def fetch_addresses(cur) -> list[str]:
    cur.execute("SELECT address FROM addresses ORDER BY address")
    return [address for (address,) in cur.fetchall() if ETH_ADDRESS.match(address)]


def fetch_tags(addresses: list[str]) -> dict[str, list[dict]]:
    """Lowercased address -> its tags, for every address that has any.
    Looks addresses up BATCH_SIZE per request, REQUEST_INTERVAL apart."""
    unique = sorted({a.lower() for a in addresses})
    tags: dict[str, list[dict]] = {}
    for start in range(0, len(unique), BATCH_SIZE):
        if start:
            time.sleep(REQUEST_INTERVAL)
        batch = unique[start : start + BATCH_SIZE]
        response = requests.get(
            METADATA_URL,
            params={"addresses": ",".join(batch), "chainId": ETHEREUM_CHAIN_ID},
            timeout=TIMEOUT,
        )
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
        # Keyed by checksum-case address; untagged addresses are left out.
        for address, entry in (response.json().get("addresses") or {}).items():
            tags[address.lower()] = entry.get("tags") or []
        print(".", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    return tags


def _first_and_rest(values: list[str]) -> tuple[str, str]:
    return (values[0] if values else ""), "; ".join(values[1:])


def row_for(address: str, tags: list[dict]) -> list[str]:
    """The output columns for one address."""
    names = [t for t in tags if t.get("tagType") == "name"]
    names.sort(key=lambda t: -(t.get("ordinal") or 0))
    others = [t for t in tags if t.get("tagType") not in ("name", "note")]
    notes = [_meta(t).get("data") or t.get("name") for t in tags if t.get("tagType") == "note"]
    name, second_name = _first_and_rest([t.get("name") or "" for t in names])
    tag, second_tag = _first_and_rest([t.get("name") or "" for t in others])
    return [address, name, second_name, tag, second_tag, "; ".join(notes)]


def print_table(rows: list[list[str]]) -> None:
    """The rows, with every column but the last padded to line up."""
    table = [COLUMNS] + rows
    widths = [max(len(row[i]) for row in table) for i in range(len(COLUMNS) - 1)]
    for row in table:
        cells = [cell.ljust(width) for cell, width in zip(row, widths)] + [row[-1]]
        print("  ".join(cells).rstrip())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_file", type=Path, help="the CSV file to write (overwritten if it exists)")
    args = parser.parse_args()

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
            addresses = fetch_addresses(cur)
    finally:
        conn.close()

    print(f"looking up {len(addresses)} Ethereum-style address(es)", file=sys.stderr)
    try:
        tags = fetch_tags(addresses)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"\nlookup failed: {exc}", file=sys.stderr)
        sys.exit(1)

    rows = [row_for(address, tags.get(address.lower(), [])) for address in addresses]
    with args.csv_file.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        writer.writerows(rows)
    print_table(rows)
    print(f"wrote {len(rows)} row(s) to {args.csv_file}", file=sys.stderr)


if __name__ == "__main__":
    main()
