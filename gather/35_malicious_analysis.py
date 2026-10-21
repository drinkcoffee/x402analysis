#!/usr/bin/env python3
"""Finds the addresses in dataset/metadata_base.json (written by
09_fetch_metadata_base.py) whose Blockscout name or tags include "phish",
ignoring case -- so "Phish / Hack" tags and "Fake_Phishing..." names both
count (see gatherlib.phishing) -- and prints them.

Prints a row per address, sorted by address, tab-separated: the address,
and which of facilitator, server, client and funder it is. A funder's row
is followed by the addresses it funded, from dataset/funders.json (or its
pieces; see gatherlib.funders), one per line, indented two spaces and
sorted by address, each with which of those it is, and -- for a client
already listed under an earlier funder -- a third column, "duplicate".
A funded server also has a fourth column, the server's api_url, and a
fifth, the categories of the services it offers (unique, alphabetical,
comma-separated), from dataset/services-annotated.json; an address that
is the payment address of several servers gets a line per server, sorted
by api_url (all with the same first three columns). Columns that would be
empty at the end of a line are left off. Prints "None" if there are none.
Counts go to standard error.

--only KIND [KIND ...] shows only addresses that are one of those kinds
(facilitator, server, client, funder): only those funded addresses are
listed, and a phishing address's own line is kept if it's one of them or
any address it funded is listed -- so --only server shows the servers the
phishing funders funded, under the funders that funded them, and --only
funder shows the phishing funders alone.

Doesn't use the database. Can be run any number of times.

Usage:
    python gather/35_malicious_analysis.py [--only KIND [KIND ...]]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

GATHER_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(GATHER_ROOT))

from gatherlib import DATASET_DIR  # noqa: E402
from gatherlib.funders import load_funders  # noqa: E402
from gatherlib.phishing import WORD, load_metadata, phishing_entries  # noqa: E402

SERVICES_PATH = DATASET_DIR / "services-annotated.json"
KIND_ORDER = ("facilitator", "server", "client", "funder")


def kinds_text(kinds) -> str:
    return ", ".join(kind for kind in KIND_ORDER if kind in (kinds or []))


def normalise(address: str) -> str:
    """Ethereum-style addresses in lower case; any other as it is."""
    return address.lower() if address[:2].lower() == "0x" else address


def servers_by_address() -> dict[str, list[tuple[str, str]]]:
    """Normalised payment address -> (api_url, its categories) for each
    server in SERVICES_PATH with that address, sorted by api_url."""
    if not SERVICES_PATH.exists():
        sys.exit(f"{SERVICES_PATH.relative_to(GATHER_ROOT)} doesn't exist.")
    servers: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for server in json.loads(SERVICES_PATH.read_text())["servers"]:
        categories = sorted({service.get("category") or "uncategorised" for service in server.get("services") or []})
        for address in server.get("addresses") or {}:
            servers[normalise(address)].append((server.get("api_url") or "", ", ".join(categories)))
    return {address: sorted(found) for address, found in servers.items()}


def row(*columns: str) -> str:
    """`columns` tab-separated, leaving off empty ones at the end."""
    columns = list(columns)
    while columns and not columns[-1]:
        columns.pop()
    return "\t".join(columns)


def funded_lines(items: list[dict], servers: dict, listed: set[str], only: set[str]) -> tuple[list[str], int, int]:
    """The indented lines for a funder's funded addresses `items`, keeping
    those that are one of the kinds in `only` (all if it's empty), with
    how many addresses they are and how many are marked duplicate.
    `listed`: the addresses already listed, updated."""
    lines, count, duplicates = [], 0, 0
    for item in sorted(items, key=lambda item: item["address"]):
        kinds = item.get("kinds") or []
        if only and not only & set(kinds):
            continue
        duplicate = "duplicate" if item["address"] in listed and "client" in kinds else ""
        address = f"  {item['address']}"
        if "server" in kinds:
            for api_url, categories in servers.get(normalise(item["address"])) or [("", "")]:
                lines.append(row(address, kinds_text(kinds), duplicate, api_url, categories))
        else:
            lines.append(row(address, kinds_text(kinds), duplicate))
        duplicates += bool(duplicate)
        listed.add(item["address"])
        count += 1
    return lines, count, duplicates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--only",
        nargs="+",
        choices=KIND_ORDER,
        metavar="KIND",
        help=f"only show addresses that are one of these kinds ({', '.join(KIND_ORDER)})",
    )
    only = set(parser.parse_args().only or [])
    addresses = load_metadata()
    found = phishing_entries(addresses)
    funded = {}
    if any("funder" in (entry.get("kinds") or []) for entry in found):
        wanted = {entry["address"] for entry in found}
        funded = {f["address"]: f.get("funded") or [] for f in load_funders() if f["address"] in wanted}
    servers = {}
    if any("server" in (item.get("kinds") or []) for items in funded.values() for item in items):
        servers = servers_by_address()
    shown = funded_count = duplicates = 0
    listed: set[str] = set()
    for entry in found:
        kinds = entry.get("kinds") or []
        lines, count, marked = ([], 0, 0)
        if "funder" in kinds:
            lines, count, marked = funded_lines(funded.get(entry["address"], []), servers, listed, only)
        if only and not only & set(kinds) and not lines:
            continue
        print(row(entry["address"], kinds_text(kinds)))
        for line in lines:
            print(line)
        shown += 1
        funded_count += count
        duplicates += marked
    if not shown:
        print("None")
    print(
        f"{len(found):,} of {len(addresses):,} address(es) with metadata have \"{WORD}\" in their name or tags"
        + (f" ({shown:,} shown: --only {' '.join(k for k in KIND_ORDER if k in only)})" if only else "")
        + f"; listed {funded_count:,} address(es) they funded, {duplicates:,} of them clients already listed (duplicate)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
