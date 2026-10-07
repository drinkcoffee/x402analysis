#!/usr/bin/env python3
"""Finds who has paid an address in USDC on Base: every unique sender and
the total USDC each sent, from Blockscout's Base explorer.

Only real USDC counts -- Circle's contract on Base,
0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913. Other tokens, including spam
tokens named to look like USDC, are ignored.

Uses the same API the explorer's token transfers tab uses
(https://base.blockscout.com/address/<address>?tab=token_transfers):

    GET https://base.blockscout.com/api/v2/addresses/<address>/token-transfers
        ?filter=to&token=<USDC contract>

which returns 50 transfers a page, newest first, plus `next_page_params`
to pass back for the next page (null on the last). Every page is fetched.
No API key is needed, but the API sits behind a Cloudflare bot check that
blocks plain scripted requests, so requests send the browser-like headers
the explorer page does. It's also rate limited: when the
x-ratelimit-remaining header runs out, or a request gets HTTP 429, this
waits for the limit to reset and carries on. A "." is printed to standard
error per page.

Output, to standard out: the number of transfers, senders and total USDC,
then each sender with how many transfers they made and the total USDC they
sent, largest first.

Usage:
    python scripts/l_find_users_scrape_blockscout.py <address>

e.g. python scripts/l_find_users_scrape_blockscout.py 0x400D65Bb174C546ed92F5D61cE21FbDe96b8bAcC
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections import defaultdict
from decimal import Decimal

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

EXPLORER = "https://base.blockscout.com"
TRANSFERS_URL = EXPLORER + "/api/v2/addresses/{address}/token-transfers"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6
ETH_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130 Safari/537.36"
)
TIMEOUT = 30.0
MAX_RETRIES = 5
DEFAULT_RETRY_WAIT = 60.0


def _reset_wait(response: requests.Response) -> float:
    """Seconds until the rate limit resets. Blockscout gives
    x-ratelimit-reset in milliseconds."""
    try:
        return int(response.headers["x-ratelimit-reset"]) / 1000 + 1
    except (KeyError, ValueError):
        return DEFAULT_RETRY_WAIT


def get_page(session: requests.Session, url: str, params: dict) -> dict:
    """One page of transfers, waiting out the rate limit if needed."""
    for _attempt in range(MAX_RETRIES):
        response = session.get(url, params=params, timeout=TIMEOUT)
        if response.status_code == 429:
            wait = _reset_wait(response)
            print(f"\nrate limited -- waiting {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if response.status_code == 403 and "cloudflare" in response.headers.get("server", ""):
            raise RuntimeError("HTTP 403 from Cloudflare's bot check -- the explorer is refusing scripted requests")
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
        if response.headers.get("x-ratelimit-remaining") == "0":
            wait = _reset_wait(response)
            print(f"\nrate limit reached -- waiting {wait:.0f}s", file=sys.stderr, flush=True)
            time.sleep(wait)
        return response.json()
    raise RuntimeError(f"still rate limited after {MAX_RETRIES} attempts")


def _print_dot(_page_number: int) -> None:
    print(".", end="", file=sys.stderr, flush=True)


def fetch_transfers(address: str, on_page=_print_dot) -> list[dict]:
    """Every USDC transfer to `address`, across all pages. Calls
    `on_page(page number)` after each page is fetched -- by default, prints
    a "." to standard error -- and ends with a newline on standard error."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Referer": f"{EXPLORER}/address/{address}?tab=token_transfers",
        }
    )
    url = TRANSFERS_URL.format(address=address)
    params: dict = {"filter": "to", "token": USDC}
    transfers: list[dict] = []
    page_number = 0
    while True:
        page = get_page(session, url, params)
        # The API filters by token already; this is belt and braces, so
        # nothing but real USDC is ever counted.
        transfers.extend(
            item for item in page.get("items") or [] if ((item.get("token") or {}).get("address_hash") or "").lower() == USDC.lower()
        )
        page_number += 1
        on_page(page_number)
        next_params = page.get("next_page_params")
        if not next_params:
            break
        params = {**next_params, "filter": "to", "token": USDC}
    print(file=sys.stderr)
    return transfers


def amount_of(transfer: dict) -> Decimal:
    """The transfer's amount in whole USDC."""
    return Decimal((transfer.get("total") or {}).get("value") or 0).scaleb(-USDC_DECIMALS)


def _format(amount: Decimal) -> str:
    text = f"{amount:,f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def totals_by_sender(transfers: list[dict], min_amount: Decimal = Decimal(0)) -> dict[str, list]:
    """sender -> [transfer count, total USDC], counting only transfers of
    at least `min_amount` USDC."""
    senders: dict[str, list] = defaultdict(lambda: [0, Decimal(0)])
    for transfer in transfers:
        amount = amount_of(transfer)
        if amount < min_amount:
            continue
        entry = senders[(transfer.get("from") or {}).get("hash") or "?"]
        entry[0] += 1
        entry[1] += amount
    return dict(senders)


def sorted_senders(senders: dict[str, list]) -> list[tuple[str, list]]:
    """Senders largest total first, then most transfers."""
    return sorted(senders.items(), key=lambda item: (-item[1][1], -item[1][0], item[0]))


def report(address: str, transfers: list[dict]) -> None:
    senders = totals_by_sender(transfers)

    total = sum((t for _c, t in senders.values()), Decimal(0))
    print(f"USDC transfers to {address} on Base")
    print(f"{sum(c for c, _t in senders.values())} transfer(s) from {len(senders)} unique address(es), total {_format(total)} USDC")
    if not senders:
        return
    print()
    count_width = max(len("transfers"), *(len(str(c)) for c, _t in senders.values()))
    total_width = max(len("USDC"), *(len(_format(t)) for _c, t in senders.values()))
    print(f"{'address':<42}  {'transfers':>{count_width}}  {'USDC':>{total_width}}")
    for sender, (c, t) in sorted_senders(senders):
        print(f"{sender:<42}  {c:>{count_width}}  {_format(t):>{total_width}}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("address", help="the Base address to find USDC senders to (0x + 40 hex digits)")
    args = parser.parse_args()

    if not ETH_ADDRESS.match(args.address):
        parser.error(f"not an Ethereum-style address: {args.address}")

    try:
        transfers = fetch_transfers(args.address)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"\nlookup failed: {exc}", file=sys.stderr)
        sys.exit(1)
    report(args.address, transfers)


if __name__ == "__main__":
    main()
