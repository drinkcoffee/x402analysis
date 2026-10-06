#!/usr/bin/env python3
"""Prints the tags Blockscout's Ethereum explorer shows for one address --
the ones beside "Address Details" on https://eth.blockscout.com/address/<address>
(e.g. "Nomad Bridge Exploiter 3" and "Exploit"), plus the warning banner
note some addresses carry.

Those tags aren't in the page's HTML: the page is a JavaScript app that
fetches them after loading, from Blockscout's public metadata service.
This calls that service directly, the same way the page does:

    GET https://metadata.services.blockscout.com/api/v1/metadata
        ?addresses=<address>&chainId=1

It needs no API key or payment. Always Ethereum mainnet.

Output, to standard out: the address, then one line per tag --
"<name> [<tag type>]", with any extra info the tag carries -- and the
note(s), or "No Public Tags".

Usage:
    python scripts/l_bc_address_screen_scrape_blockscout.py <address>

e.g. python scripts/l_bc_address_screen_scrape_blockscout.py 0xB5C55f76f90Cc528B2609109Ca14d8d84593590E
"""

from __future__ import annotations

import argparse
import json
import re
import sys

try:
    import requests
except ImportError as exc:
    print(
        f"missing required package: {exc.name}. Run `pip install -r "
        "requirements.txt` from x402-analysis-api/, with your virtualenv "
        "active.",
        file=sys.stderr,
    )
    sys.exit(1)

METADATA_URL = "https://metadata.services.blockscout.com/api/v1/metadata"
ETHEREUM_CHAIN_ID = 1
ETH_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
TIMEOUT = 20.0


def fetch_tags(address: str) -> list[dict]:
    """The address's tags from the metadata service; [] if it has none."""
    response = requests.get(
        METADATA_URL,
        params={"addresses": address, "chainId": ETHEREUM_CHAIN_ID},
        timeout=TIMEOUT,
    )
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    # Keyed by the address in checksum case, whatever case was asked for;
    # an address with no tags is left out ({"addresses": {}}).
    entries = response.json().get("addresses") or {}
    return next(iter(entries.values()), {}).get("tags") or []


def _meta(tag: dict) -> dict:
    """A tag's `meta`, which comes as a JSON-encoded string."""
    try:
        meta = json.loads(tag.get("meta") or "{}")
    except ValueError:
        return {}
    return meta if isinstance(meta, dict) else {}


def describe(tags: list[dict]) -> list[str]:
    """One line per tag, name tags first (as the page shows them), then
    other tags, then notes (the page's warning banner)."""
    labels = [t for t in tags if t.get("tagType") != "note"]
    notes = [t for t in tags if t.get("tagType") == "note"]
    labels.sort(key=lambda t: (t.get("tagType") != "name", -(t.get("ordinal") or 0)))

    lines = []
    for tag in labels:
        line = f"{tag.get('name')} [{tag.get('tagType')}]"
        info = _meta(tag).get("info")
        if info:
            line += " -- " + ", ".join(info if isinstance(info, list) else [str(info)])
        lines.append(line)
    for tag in notes:
        meta = _meta(tag)
        status = meta.get("alertStatus")
        lines.append(f"Note{f' ({status})' if status else ''}: {meta.get('data') or tag.get('name')}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("address", help="the address to look up (0x + 40 hex digits)")
    args = parser.parse_args()

    if not ETH_ADDRESS.match(args.address):
        parser.error(f"not an Ethereum-style address: {args.address}")

    try:
        tags = fetch_tags(args.address)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"lookup failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(args.address)
    for line in describe(tags) or ["No Public Tags"]:
        print(f"  {line}")


if __name__ == "__main__":
    main()
