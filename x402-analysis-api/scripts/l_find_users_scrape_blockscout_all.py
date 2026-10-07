#!/usr/bin/env python3
"""Finds who has paid each server in USDC on Base: for every Ethereum-style
address (0x + 40 hex digits) linked to a server in the local Postgres
mirror (localdb/), every sender of real USDC to it and how much they sent.
Read-only.

Each address is checked the way l_find_users_scrape_blockscout.py checks
one: every page of its incoming USDC transfers, from Blockscout's Base
explorer API, with no cap on pages. Every address is checked on Base,
whatever chain it's recorded as being on. An address stored more than once
in different letter case is checked once.

Output, to the screen and to a CSV file, one row per server address and
sender, with columns:
    server address, associated address, transfers, USDC, server names
  - associated address: an address that sent the server address USDC
  - transfers / USDC: how many transfers it made and the USDC they total
  - server names: every server the address is linked to, "; "-separated
A server address that received no USDC gets one row with no associated
address and 0 transfers. Senders are listed largest total first.

Rows are printed and written to the CSV as each address finishes, so an
interrupted run (Ctrl-C) keeps everything found so far. Progress goes to
standard error: "[n/total] <address>", a "." per page of transfers (with a
page count every 20 pages), then that address's totals; and a message
whenever the run is waiting on Blockscout's rate limit (150 requests per
window of about 80 seconds). An address that can't be checked is reported
and skipped, and listed again at the end; five failures in a row stop the
run.

Usage:
    python scripts/l_find_users_scrape_blockscout_all.py <csv file> [--min-amount 0.01]

--min-amount ignores transfers below that many USDC -- e.g. the dust that
address-poisoning lookalikes send. By default every transfer counts.
"""

from __future__ import annotations

import argparse
import csv
import sys
from decimal import Decimal
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
from l_find_users_scrape_blockscout import (  # noqa: E402
    _format,
    fetch_transfers,
    sorted_senders,
    totals_by_sender,
)

COLUMNS = ["server address", "associated address", "transfers", "USDC", "server names"]
PAGE_COUNT_EVERY = 20
MAX_CONSECUTIVE_FAILURES = 5


def fetch_server_addresses(cur) -> list[tuple[str, list[str]]]:
    """(address, server names) for every Ethereum-style address linked to
    a server, once per address ignoring case, sorted by address."""
    cur.execute(
        """
        SELECT a.address, s.name
        FROM linkaddresses l
        JOIN addresses a ON a.id = l.address
        JOIN server s ON s.id = l.ref
        WHERE l.owner = 1 AND a.address ~ '^0x[0-9a-fA-F]{40}$'
        """
    )
    spellings: dict[str, set[str]] = {}
    names: dict[str, set[str]] = {}
    for address, name in cur.fetchall():
        key = address.lower()
        spellings.setdefault(key, set()).add(address)
        names.setdefault(key, set()).add(name)
    result = []
    for key in sorted(spellings):
        # Prefer a checksum (mixed-case) spelling over an all-lowercase one.
        address = max(spellings[key], key=lambda a: (a != a.lower(), a))
        result.append((address, sorted(names[key])))
    return result


def _page_progress(page_number: int) -> None:
    if page_number % PAGE_COUNT_EVERY == 0:
        print(f"({page_number} pages)", end="", file=sys.stderr, flush=True)
    else:
        print(".", end="", file=sys.stderr, flush=True)


def _plain(amount: Decimal) -> str:
    """The amount with no thousands separators, for the CSV."""
    text = f"{amount:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _print_row(row: list[str]) -> None:
    server, sender, count, usdc, names = row
    try:
        usdc = _format(Decimal(usdc))
    except ArithmeticError:
        pass  # the header
    print(f"{server:<42}  {sender:<42}  {count:>9}  {usdc:>14}  {names}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_file", type=Path, help="the CSV file to write (overwritten if it exists)")
    parser.add_argument(
        "--min-amount",
        type=Decimal,
        default=Decimal(0),
        help="ignore transfers of less than this many USDC (default: count every transfer)",
    )
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
            addresses = fetch_server_addresses(cur)
    finally:
        conn.close()

    total = len(addresses)
    print(f"checking {total} server address(es) for USDC transfers on Base", file=sys.stderr)
    failed: list[tuple[str, str]] = []
    consecutive_failures = 0
    checked = 0
    with args.csv_file.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        _print_row(COLUMNS)
        try:
            for n, (address, names) in enumerate(addresses, 1):
                print(f"[{n}/{total}] {address} ", end="", file=sys.stderr, flush=True)
                try:
                    transfers = fetch_transfers(address, on_page=_page_progress)
                except (requests.RequestException, RuntimeError, ValueError) as exc:
                    print(f"\n  failed: {exc}", file=sys.stderr)
                    failed.append((address, str(exc)))
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        print(f"stopping: {consecutive_failures} failures in a row", file=sys.stderr)
                        break
                    continue
                consecutive_failures = 0
                checked += 1

                senders = totals_by_sender(transfers, args.min_amount)
                count = sum(c for c, _t in senders.values())
                usdc = sum((t for _c, t in senders.values()), Decimal(0))
                print(
                    f"  {count} transfer(s) from {len(senders)} sender(s), {_format(usdc)} USDC",
                    file=sys.stderr,
                )
                joined_names = "; ".join(names)
                rows = [[address, sender, str(c), _plain(t), joined_names] for sender, (c, t) in sorted_senders(senders)]
                for row in rows or [[address, "", "0", "0", joined_names]]:
                    writer.writerow(row)
                    _print_row(row)
                f.flush()
        except KeyboardInterrupt:
            print(f"\ninterrupted -- {args.csv_file} has the {checked} address(es) checked so far", file=sys.stderr)
            sys.exit(130)

    print(f"checked {checked} of {total} address(es); wrote {args.csv_file}", file=sys.stderr)
    if failed:
        print(f"{len(failed)} address(es) couldn't be checked:", file=sys.stderr)
        for address, error in failed:
            print(f"  {address}: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
