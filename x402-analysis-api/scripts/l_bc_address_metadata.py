#!/usr/bin/env python3
"""Looks up public tags for every address in the local Postgres mirror's
(localdb/) addresses table, from Etherscan (the default) or Blockscout
(--blockscout), printing one line per address. Read-only.

For each address:
  - not an Ethereum-style address (0x + 40 hex digits) -> "Not Eth Address"
  - otherwise, look it up on the chain the address is recorded as being
    on (addresses.chains, a comma-separated list):
      - on all chains ("*" / "eip155:*"), or no chain recorded -> Base
      - on several chains -> the first one listed
      - any lookup that would be on Base (including the two cases above
        that default to it) is made on Ethereum mainnet instead
    and print its public tags, or "No Public Tags":
      - Etherscan: the V2 "Get Address Name Tag" endpoint
        (GET https://api.etherscan.io/v2/api?module=nametag
        &action=getaddresstag&chainid=<id>&address=<address>) -- prints the
        name tag (Etherscan's display name for the address) and labels.
      - Blockscout (--blockscout): GET https://api.blockscout.com/{chainId}
        /api/v2/addresses/{address} -- prints each public tag's label and
        display name. Blockscout reports tags in two places, the older
        `public_tags` list (label/display_name) and the newer
        `metadata.tags` (slug/name), and both are shown.
  - a chain that isn't an EVM chain that can be asked about (e.g.
    "solana"), or a request that fails, prints why instead.

Requests are rate limited to one per second.

Needs an API key for whichever is used -- ETHERSCAN_API_KEY or
BLOCKSCOUT_API_KEY -- read from a .env file in either this project's root
or cli-tool's (same as check_supported.py's CDP keys). Etherscan's name tag
endpoint is "API Exclusive" (a paid plan); without access, every lookup
comes back with an error saying so. Blockscout's free plan doesn't include
some chains (Base and Polygon among them), and requests for those come
back with an error naming the plans that do.

Usage:
    python scripts/l_bc_address_metadata.py [--blockscout]
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI_TOOL_ROOT = PROJECT_ROOT.parent / "cli-tool"

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
load_dotenv(CLI_TOOL_ROOT / ".env")

import os  # noqa: E402

import download_db  # noqa: E402
import preflight  # noqa: E402

sys.path.insert(0, str(CLI_TOOL_ROOT))
try:
    from x402tool.etherscan_client import resolve_chain_id  # noqa: E402
except ImportError as exc:
    print(
        f"couldn't import the sibling cli-tool project's x402tool.etherscan_client "
        f"module ({exc}). Clone cli-tool next to this project (as it is in "
        f"this monorepo) -- expected at {CLI_TOOL_ROOT}.",
        file=sys.stderr,
    )
    sys.exit(1)

ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
BLOCKSCOUT_URL = "https://api.blockscout.com/{chain_id}/api/v2/addresses/{address}"
BASE_CHAIN_ID = 8453
ETHEREUM_CHAIN_ID = 1
ALL_CHAINS = {"*", "eip155:*"}
ETH_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
REQUEST_INTERVAL = 1.0
TIMEOUT = 20.0


def fetch_addresses(cur) -> list[tuple[str, str | None]]:
    cur.execute("SELECT address, chains FROM addresses ORDER BY address")
    return cur.fetchall()


def chain_for(chains: str | None) -> tuple[int | None, str]:
    """(chain id, chain as written) for the chain to query: Base for "on
    all chains" or nothing recorded, otherwise the first one listed. The
    chain id is None if that chain isn't an EVM chain that can be asked
    about."""
    first = next((c.strip() for c in (chains or "").split(",") if c.strip()), None)
    if first is None or first in ALL_CHAINS:
        chain_id, chain = BASE_CHAIN_ID, "base"
    else:
        chain_id, chain = resolve_chain_id(first), first
    # Base lookups are made against Ethereum mainnet instead.
    if chain_id == BASE_CHAIN_ID:
        return ETHEREUM_CHAIN_ID, f"{chain}->ethereum"
    return chain_id, chain


def describe_etherscan_tags(results: list[dict]) -> str:
    """The address's name tag and labels, e.g. "Binance 14 (labels:
    Binance, Exchange)", or "No Public Tags"."""
    tags = []
    for result in results:
        nametag = result.get("nametag") or ""
        labels = ", ".join(result.get("labels") or [])
        if nametag and labels:
            tags.append(f"{nametag} (labels: {labels})")
        elif nametag or labels:
            tags.append(nametag or f"labels: {labels}")
    return "; ".join(tags) if tags else "No Public Tags"


def describe_blockscout_tags(data: dict) -> str:
    """The address's public tags as "label (display name)" entries, or
    "No Public Tags"."""
    tags = []
    for tag in data.get("public_tags") or []:
        tags.append(f"{tag.get('label')} ({tag.get('display_name')})")
    for tag in (data.get("metadata") or {}).get("tags") or []:
        tags.append(f"{tag.get('slug')} ({tag.get('name')})")
    return "; ".join(tags) if tags else "No Public Tags"


class RateLimiter:
    def __init__(self, interval: float):
        self.interval = interval
        self.last = 0.0

    def wait(self) -> None:
        delay = self.last + self.interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.last = time.monotonic()


def lookup_etherscan(api_key: str, chain_id: int, address: str, limiter: RateLimiter) -> str:
    limiter.wait()
    try:
        response = requests.get(
            ETHERSCAN_URL,
            params={
                "chainid": chain_id,
                "module": "nametag",
                "action": "getaddresstag",
                "address": address,
                "apikey": api_key,
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        return f"Error: {type(exc).__name__}"
    try:
        data = response.json()
    except ValueError:
        return f"Error: HTTP {response.status_code}, non-JSON response"
    if not isinstance(data, dict):
        return f"Error: HTTP {response.status_code}, unexpected response"
    result = data.get("result")
    if str(data.get("status")) != "1":
        # Etherscan reports "no tag for this address" as a failed status
        # with an empty result, rather than as an empty success.
        if not result or (isinstance(result, str) and "no data" in result.lower()):
            return "No Public Tags"
        return f"Error: {result if isinstance(result, str) else data.get('message')}"
    return describe_etherscan_tags(result if isinstance(result, list) else [result])


def lookup_blockscout(api_key: str, chain_id: int, address: str, limiter: RateLimiter) -> str:
    limiter.wait()
    try:
        response = requests.get(
            BLOCKSCOUT_URL.format(chain_id=chain_id, address=address),
            params={"apikey": api_key},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        return f"Error: {type(exc).__name__}"
    try:
        data = response.json()
    except ValueError:
        return f"Error: HTTP {response.status_code}, non-JSON response"
    if not response.ok or not isinstance(data, dict):
        message = (data.get("error") or data.get("message")) if isinstance(data, dict) else None
        return f"Error: HTTP {response.status_code}" + (f", {message}" if message else "")
    return describe_blockscout_tags(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--blockscout", action="store_true", help="use Blockscout's API instead of Etherscan's")
    args = parser.parse_args()

    if args.blockscout:
        key_name, key_url, lookup = "BLOCKSCOUT_API_KEY", "https://dev.blockscout.com/", lookup_blockscout
    else:
        key_name, key_url, lookup = "ETHERSCAN_API_KEY", "https://etherscan.io/apis", lookup_etherscan
    api_key = os.getenv(key_name)
    preflight.check_or_exit(
        preflight.require_env(
            key_name,
            f"get an API key at {key_url} and add it to x402-analysis-api/.env or cli-tool/.env",
        ),
        download_db.preflight_checks,
    )
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

    limiter = RateLimiter(REQUEST_INTERVAL)
    for address, chains in addresses:
        if not ETH_ADDRESS.match(address):
            result = "Not Eth Address"
        else:
            chain_id, chain = chain_for(chains)
            if chain_id is None:
                result = f"Unsupported chain: {chain}"
            else:
                result = f"[{chain}] " + lookup(api_key, chain_id, address, limiter)
        print(f"{address}  {result}", flush=True)


if __name__ == "__main__":
    main()
