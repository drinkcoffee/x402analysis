#!/usr/bin/env python3
"""Checks every gathered facilitator, server, client and funder address
against the sanctions lists in dataset/sanctions_list.json (written by
20_gather_sanctions_lists.py), and prints those that are on them.

Addresses checked, from dataset/:
  - facilitator: facilitators.json's addresses (01_facilitators.py).
  - server: services.json's servers' addresses (02_services.py).
  - client: clients.json's clients (05_find_clients_base.py).
  - funder: funders.json's funders (07_find_funders_base.py), or, if
    funders.json isn't there, the funders.part*.json pieces
    40_split_funders_file.py splits it into. The addresses they funded
    are counted as the kinds funders.json says they are.

Base and Solana addresses are both checked: Ethereum-style (0x...)
addresses ignoring case, and any other address exactly, against every
blockchain address on the lists, whatever currency it's listed under.

Prints a row per gathered address that's on a list, sorted by address,
tab-separated: the address; which of facilitator, server, client and
funder it is; and what it's listed as ("<list>: <name> (<programs>)",
several joined with "; "). Prints "None" if there are none. A summary of
what was checked goes to standard error.

Doesn't use the database. Can be run any number of times.

Usage:
    python gather/34_check_sanctions_base.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

GATHER_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(GATHER_ROOT))

from gatherlib import DATASET_DIR  # noqa: E402

SANCTIONS_PATH = DATASET_DIR / "sanctions_list.json"
FACILITATORS_PATH = DATASET_DIR / "facilitators.json"
SERVICES_PATH = DATASET_DIR / "services.json"
CLIENTS_PATH = DATASET_DIR / "clients.json"
FUNDERS_PATH = DATASET_DIR / "funders.json"
FUNDERS_PIECES = "funders.part*.json"
KIND_ORDER = ("facilitator", "server", "client", "funder")


def normalise(address: str) -> str:
    """Ethereum-style addresses in lower case; any other as it is."""
    address = address.strip()
    return address.lower() if address[:2].lower() == "0x" else address


def read_json(path: Path, made_by: str):
    if not path.exists():
        sys.exit(f"{path.relative_to(GATHER_ROOT)} doesn't exist -- run {made_by} first.")
    return json.loads(path.read_text())


def funders() -> list[dict]:
    """funders.json's funders, or the pieces' if it isn't there."""
    if FUNDERS_PATH.exists():
        return json.loads(FUNDERS_PATH.read_text())["funders"]
    pieces = sorted(DATASET_DIR.glob(FUNDERS_PIECES))
    if not pieces:
        sys.exit(
            f"neither {FUNDERS_PATH.relative_to(GATHER_ROOT)} nor its pieces exist -- run 07_find_funders_base.py first."
        )
    return [funder for piece in pieces for funder in json.loads(piece.read_text())["funders"]]


def gathered() -> dict[str, set[str]]:
    """Normalised address -> the kinds it is, for every gathered address."""
    kinds: dict[str, set[str]] = defaultdict(set)
    for facilitator in read_json(FACILITATORS_PATH, "01_facilitators.py")["facilitators"]:
        for address in facilitator.get("addresses") or []:
            kinds[normalise(address)].add("facilitator")
    for server in read_json(SERVICES_PATH, "02_services.py")["servers"]:
        for address in server.get("addresses") or {}:
            kinds[normalise(address)].add("server")
    for client in read_json(CLIENTS_PATH, "05_find_clients_base.py")["clients"]:
        kinds[normalise(client["address"])].add("client")
    for funder in funders():
        address = normalise(funder["address"])
        kinds[address].add("funder")
        kinds[address].update(funder.get("funder_is") or [])
        for funded in funder.get("funded") or []:
            kinds[normalise(funded["address"])].update(funded.get("kinds") or [])
    return kinds


def sanctioned(sanctions: dict) -> dict[str, list[str]]:
    """Normalised blockchain address -> what it's listed as, on every
    list in `sanctions`."""
    listed: dict[str, list[str]] = defaultdict(list)
    for entry in sanctions["entries"]:
        programs = ", ".join(entry.get("programs") or [])
        what = f"{entry['list']}: {entry['name']}" + (f" ({programs})" if programs else "")
        for item in entry.get("blockchain_addresses") or []:
            address = normalise(item.get("address") or "")
            if address and what not in listed[address]:
                listed[address].append(what)
    return listed


def matches(kinds: dict[str, set[str]], listed: dict[str, list[str]]) -> list[tuple[str, list[str], list[str]]]:
    """(address, its kinds, what it's listed as) for every gathered address
    on a list, sorted by address."""
    return [
        (address, [kind for kind in KIND_ORDER if kind in kinds[address]], listed[address])
        for address in sorted(set(kinds) & set(listed))
    ]


def main() -> None:
    sanctions = read_json(SANCTIONS_PATH, "20_gather_sanctions_lists.py")
    listed = sanctioned(sanctions)
    kinds = gathered()
    found = matches(kinds, listed)
    for address, address_kinds, what in found:
        print(f"{address}\t{', '.join(address_kinds)}\t{'; '.join(what)}")
    if not found:
        print("None")
    counts = ", ".join(f"{sum(1 for k in kinds.values() if kind in k):,} {kind}s" for kind in KIND_ORDER)
    lists = ", ".join(f"{s['list']} published {s['published']}" for s in sanctions["sources"])
    print(
        f"checked {len(kinds):,} address(es) ({counts}) against {len(listed):,} sanctioned address(es) "
        f"({lists}): {len(found):,} on a list",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
