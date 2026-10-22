#!/usr/bin/env python3
"""Finds the addresses in dataset/metadata_base_phishing_only_annotated.json
(09_fetch_metadata_base.py's Blockscout metadata, cut down to the phishing
addresses by 09a_extract_phishing.py, then reviewed by hand) whose name or
tags include "phish", ignoring case -- so "Phish / Hack" tags and "Fake_Phishing..." names both
count (see gatherlib.phishing) -- and prints them.

Prints a row per address, sorted by address, tab-separated: the address,
and which of facilitator, server, client and funder it is. A funder's row
is followed by the addresses it funded, from dataset/funders.json (or its
pieces; see gatherlib.funders), one per line, indented two spaces and
sorted by address, each with which of those it is, and -- for a client
already listed under an earlier funder -- a third column, "duplicate".
Addresses a funder sent only dust are left out: less than US$0.01 in all
(--min-funding), counting ETH at US$2,500 (--eth-usd) -- tiny sends are
what address poisoning looks like, so they mark a target, not someone the
phishing address funded (see gatherlib.phishing). So are addresses a
funder annotated with a "from" time (when it became phishing, e.g. a
compromised wallet) last sent before then; such a funder's line has a
third column, "phishing from" and the time, in UTC.
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
    python gather/35_malicious_analysis.py [--only KIND [KIND ...]] [--min-funding USD] [--eth-usd USD]
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
from gatherlib.phishing import (  # noqa: E402
    ANNOTATED_PATH,
    WORD,
    add_dust_arguments,
    entry_from,
    funded_after,
    is_dust,
    load_metadata,
    phishing_entries,
)  # noqa: E402

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
    add_dust_arguments(parser)
    args = parser.parse_args()
    only = set(args.only or [])
    addresses = load_metadata()
    found = phishing_entries(addresses)
    funded = {}
    if any("funder" in (entry.get("kinds") or []) for entry in found):
        wanted = {entry["address"] for entry in found}
        funded = {f["address"]: f.get("funded") or [] for f in load_funders() if f["address"] in wanted}
    since = {entry["address"]: entry_from(entry) for entry in found}
    dust = sum(is_dust(item, args.min_funding, args.eth_usd) for items in funded.values() for item in items)
    earlier = sum(
        not is_dust(item, args.min_funding, args.eth_usd) and not funded_after(item, since[address])
        for address, items in funded.items()
        for item in items
    )
    funded = {
        address: [
            item
            for item in items
            if not is_dust(item, args.min_funding, args.eth_usd) and funded_after(item, since[address])
        ]
        for address, items in funded.items()
    }
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
        moment = since[entry["address"]]
        print(row(entry["address"], kinds_text(kinds), f"phishing from {moment:%Y-%m-%d %H:%M:%S} UTC" if moment else ""))
        for line in lines:
            print(line)
        shown += 1
        funded_count += count
        duplicates += marked
    if not shown:
        print("None")
    print(
        f"{len(found):,} of {len(addresses):,} address(es) in dataset/{ANNOTATED_PATH.name} have \"{WORD}\" in their name or tags"
        + (f" ({shown:,} shown: --only {' '.join(k for k in KIND_ORDER if k in only)})" if only else "")
        + f"; listed {funded_count:,} address(es) they funded, {duplicates:,} of them clients already listed (duplicate)"
        + f"; ignored {dust:,} sent less than US${args.min_funding} (dust; ETH at US${args.eth_usd})"
        + f" and {earlier:,} last sent before their funder's \"from\"",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
