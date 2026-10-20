#!/usr/bin/env python3
"""Finds funders on Base -- the addresses that have sent facilitators and
servers ETH (for gas), and clients USDC (to pay with) -- and writes them
to dataset/funders.json, grouped by funding address, since one address can
fund several.

Checks, using Blockscout's Base explorer API (as 05_find_clients_base.py
does):
  - every Ethereum-style facilitator and server address, for ETH sent to
    it: transactions with a value (GET /api/v2/addresses/<address>/
    transactions?filter=to), and ETH sent by a contract during a
    transaction -- e.g. a bridge, or WETH being unwrapped (GET .../
    internal-transactions?filter=to). Failed transfers don't count.
  - every Ethereum-style client address, for real USDC sent to it (Circle's
    contract on Base): its ERC-20 transfers in (GET .../token-transfers?
    filter=to&type=ERC-20), keeping the USDC ones. With --usdc-only-query,
    it asks for just the USDC transfers instead (...&token=<USDC>), which
    is fewer pages -- but for some addresses the explorer takes about 2
    minutes to give up on that, and then the ERC-20 query is used.
An address that's more than one kind -- e.g. a server that's also a
client -- gets every check that applies. Each query reads at most 5
pages (50 items a page, newest first; --max-pages N to change) per
address, and a check that stops with more pages left saves where it
stopped, so that a later run with --more-pages can carry on from there,
reading up to that many pages more. Senders that are the zero address (ETH or USDC created out of
nothing) or the address itself aren't funders.

The API sits behind a Cloudflare bot check that blocks plain scripted
requests, so requests send the browser-like headers the explorer page
does; it's also rate limited, and when the limit's reached this waits for
it to reset and carries on (saying so).

dataset/funders.json lists each funder: what kinds of address it funds
(facilitator, server and/or client -- ETH funds an address as a
facilitator/server, USDC as a client), what it's known as itself if it's
one of those, and each address it funds -- what it funds it as, all the
kinds it is, its facilitator/server names, and the ETH and/or USDC sent:
number of transfers (for ETH, how many of them were internal), total
amount, and the first and last transfer's time. Funders are sorted by how
many addresses they fund, most first; an amount from a check that has
more pages left is marked more_pages. Server/client addresses that
couldn't be checked are listed too, and so, under incomplete, are the
checks with more pages left -- the pages read so far, the queries with
more, how many transfers were found, and no_transfers_found if none.

Doesn't change the database. Exits, doing nothing, if
dataset/funders.json exists (except with --more-pages, which rewrites
it).

Resumable: as each address is checked, what was found is saved to
localdb/progress/07_find_funders_base.jsonl, so a run that stops part way
-- Ctrl-C, a crash, or five failed addresses in a row -- carries on from
there when it's run again. An explorer outage first pauses the run:
server errors, timeouts and dropped connections are retried for about 9
minutes before an address counts as failed. dataset/funders.json is
written once every address has been checked; the progress file is then
deleted, unless some addresses failed -- then it's kept, so deleting
funders.json and running again retries just those -- or some checks have
more pages left, for --more-pages. (An earlier version of this searched
ERC-20 transfers for up to 20 pages, saving no place to carry on from;
--more-pages searches those addresses again from the start, reading
--max-pages more pages than then.)

Progress, per address checked: "[n/total] <address> <kinds> <asset>", a
"." per page of transfers (with a page count every 20 pages), then what
was found.

Usage:
    python gather/07_find_funders_base.py [--max-pages N] [--usdc-only-query] [--more-pages]
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

GATHER_ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    import psycopg2  # noqa: F401
    import requests
except ImportError as exc:
    print(
        f"missing required package: {exc.name}. Run `pip install -r gather/requirements.txt`, "
        "with gather's virtualenv active.",
        file=sys.stderr,
    )
    sys.exit(1)

load_dotenv(GATHER_ROOT / ".env")

from gatherlib import DATASET_DIR, localdb, preflight  # noqa: E402
from gatherlib.common import exit_if_exists, write_json  # noqa: E402
from gatherlib.progress import ProgressLog  # noqa: E402

OUTPUT_PATH = DATASET_DIR / "funders.json"
PROGRESS_NAME = "07_find_funders_base"
CHAIN = "base"
KINDS = {0: "facilitator", 1: "server", 2: "client"}
ETH_FUNDED = ("facilitator", "server")
USDC_FUNDED = ("client",)

EXPLORER = "https://base.blockscout.com"
ADDRESS_API = EXPLORER + "/api/v2/addresses/{address}/{path}"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDC_DECIMALS = 6
ETH_DECIMALS = 18
ZERO_ADDRESS = "0x" + "0" * 40
# Internal transaction types that move ETH; a delegatecall or staticcall
# reports a value but doesn't send it.
VALUE_CALL_TYPES = {"call", "create", "create2", "selfdestruct"}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130 Safari/537.36"
)
TIMEOUT = 130.0  # some queries take the explorer most of a minute; Cloudflare gives up at about 2
RETRY_WAITS = (15, 30, 60, 120, 300)  # seconds -- about 9 minutes in all
MAX_RATE_LIMIT_WAITS = 30
DEFAULT_RETRY_WAIT = 60.0
PAGE_COUNT_EVERY = 20
MAX_CONSECUTIVE_FAILURES = 5
DEFAULT_MAX_PAGES = 5
ITEMS_PER_PAGE = 50


def fetch_addresses(cur) -> dict[str, dict]:
    """Lower-case Ethereum-style address -> {"kinds": {kind, ...},
    "names": {name, ...}} for every facilitator, server and client
    address, sorted by address."""
    cur.execute(
        """
        SELECT lower(a.address), l.owner, coalesce(f.name, s.name)
        FROM linkaddresses l
        JOIN addresses a ON a.id = l.address
        LEFT JOIN facilitator f ON l.owner = 0 AND f.id = l.ref
        LEFT JOIN server s ON l.owner = 1 AND s.id = l.ref
        WHERE l.owner IN (0, 1, 2) AND a.address ~ '^0x[0-9a-fA-F]{40}$'
        """
    )
    addresses: dict[str, dict] = defaultdict(lambda: {"kinds": set(), "names": set()})
    for address, owner, name in cur.fetchall():
        addresses[address]["kinds"].add(KINDS[owner])
        if name:
            addresses[address]["names"].add(name)
    return dict(sorted(addresses.items()))


# --- Blockscout ----------------------------------------------------------
# Adapted from x402-analysis-api/scripts/l_find_users_scrape_blockscout.py.


def _reset_wait(response: requests.Response) -> float:
    """Seconds until the rate limit resets (x-ratelimit-reset is in ms)."""
    try:
        return int(response.headers["x-ratelimit-reset"]) / 1000 + 1
    except (KeyError, ValueError):
        return DEFAULT_RETRY_WAIT


class TooSlow(Exception):
    """The explorer took too long to answer a query (a timeout, or HTTP 524
    -- Cloudflare giving up waiting for it)."""


def get_page(session: requests.Session, url: str, params: dict, referer: str, slow_raises: bool = False) -> dict:
    """One page of results. Waits out the rate limit, and retries a server
    error (5xx), timeout or dropped connection after each of RETRY_WAITS --
    so an explorer outage of up to about 9 minutes pauses the run rather
    than failing the address. With `slow_raises`, a query the explorer
    takes too long over raises TooSlow at once instead, for the caller to
    ask another way."""
    failures = rate_limited = 0
    while True:
        try:
            response = session.get(url, params=params, timeout=TIMEOUT, headers={"Referer": referer})
        except requests.Timeout as exc:
            if slow_raises:
                raise TooSlow(type(exc).__name__)
            problem = type(exc).__name__
        except requests.ConnectionError as exc:
            problem = type(exc).__name__
        else:
            if response.status_code == 524 and slow_raises:
                raise TooSlow("HTTP 524")
            if response.status_code == 429:
                rate_limited += 1
                if rate_limited > MAX_RATE_LIMIT_WAITS:
                    raise RuntimeError(f"still rate limited after {MAX_RATE_LIMIT_WAITS} waits")
                wait = _reset_wait(response)
                print(f"\nrate limited -- waiting {wait:.0f}s", flush=True)
                time.sleep(wait)
                continue
            if response.status_code == 403 and "cloudflare" in response.headers.get("server", ""):
                raise RuntimeError("HTTP 403 from Cloudflare's bot check -- the explorer is refusing scripted requests")
            if response.ok:
                if response.headers.get("x-ratelimit-remaining") == "0":
                    wait = _reset_wait(response)
                    print(f"\nrate limit reached -- waiting {wait:.0f}s", flush=True)
                    time.sleep(wait)
                return response.json()
            if response.status_code < 500:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
            problem = f"HTTP {response.status_code}"
        if failures == len(RETRY_WAITS):
            raise RuntimeError(f"{problem}, still, after {failures} retries over {sum(RETRY_WAITS) // 60} minutes")
        wait = RETRY_WAITS[failures]
        failures += 1
        print(f"\n{problem} -- retrying in {wait}s ({failures}/{len(RETRY_WAITS)})", flush=True)
        time.sleep(wait)


class PageCounter:
    """A "." per page fetched for one address, with a count every
    PAGE_COUNT_EVERY pages."""

    def __init__(self) -> None:
        self.pages = 0

    def tick(self) -> None:
        self.pages += 1
        print(f"({self.pages} pages)" if self.pages % PAGE_COUNT_EVERY == 0 else ".", end="", flush=True)


def _hash(value) -> str:
    """An address from a Blockscout field: either a plain string or an
    object with a "hash"."""
    if isinstance(value, dict):
        value = value.get("hash")
    return (value or "").lower()


def _eth_transfer(item: dict, address: str) -> dict | None:
    if item.get("status") == "ok" and _hash(item.get("to")) == address and int(item.get("value") or 0) > 0:
        return {"from": _hash(item.get("from")), "value": int(item["value"]), "time": item.get("timestamp"), "internal": False}
    return None


def _internal_transfer(item: dict, address: str) -> dict | None:
    if (
        item.get("success") is True
        and item.get("type") in VALUE_CALL_TYPES
        and _hash(item.get("to")) == address
        and int(item.get("value") or 0) > 0
    ):
        return {"from": _hash(item.get("from")), "value": int(item["value"]), "time": item.get("timestamp"), "internal": True}
    return None


def _usdc_transfer(item: dict, address: str) -> dict | None:
    # Checked even when the explorer filtered by token, so nothing but
    # real USDC is ever counted.
    if _hash((item.get("token") or {}).get("address_hash")) != USDC or _hash(item.get("to")) != address:
        return None
    value = int((item.get("total") or {}).get("value") or 0)
    if value > 0:
        return {"from": _hash(item.get("from")), "value": value, "time": item.get("timestamp")}
    return None


# The explorer queries a check makes, by the name progress records use:
# (path under the address, query parameters, explorer tab for the
# Referer, what a list item counts as).
QUERIES = {
    "transactions": ("transactions", {"filter": "to"}, "txs", _eth_transfer),
    "internal-transactions": ("internal-transactions", {"filter": "to"}, "internal_txns", _internal_transfer),
    "usdc-transfers": ("token-transfers", {"filter": "to", "token": USDC}, "token_transfers", _usdc_transfer),
    "erc20-transfers": ("token-transfers", {"filter": "to", "type": "ERC-20"}, "token_transfers", _usdc_transfer),
}


def read_query(
    session: requests.Session,
    address: str,
    query: str,
    counter: PageCounter,
    max_pages: int,
    start: dict | None = None,
    slow_raises: bool = False,
) -> tuple[list[dict], dict | None, int]:
    """Up to `max_pages` pages (newest first) of one of QUERIES for the
    address -- from the first page, or from `start` (a page position
    saved earlier). Returns the transfers found, the position of the next
    page if there's more (else None), and how many pages were read.
    `slow_raises`: see get_page."""
    path, params, tab, transfer = QUERIES[query]
    url = ADDRESS_API.format(address=address, path=path)
    referer = f"{EXPLORER}/address/{address}?tab={tab}"
    page_params = {**start, **params} if start else dict(params)
    transfers = []
    pages = 0
    while True:
        page = get_page(session, url, page_params, referer, slow_raises)
        counter.tick()
        pages += 1
        for item in page.get("items") or []:
            found = transfer(item, address)
            if found:
                transfers.append(found)
        next_params = page.get("next_page_params")
        if not next_params:
            return transfers, None, pages
        if pages >= max_pages:
            return transfers, next_params, pages
        page_params = {**next_params, **params}


def check_address(
    session: requests.Session,
    address: str,
    asset: str,
    counter: PageCounter,
    max_pages: int,
    previous: dict | None,
    usdc_only: bool = False,
) -> tuple[list[dict], dict[str, dict], dict[str, int], bool]:
    """One address's check: its ETH ("transactions" and
    "internal-transactions") or USDC transfers in, reading up to
    `max_pages` pages of each query.

    USDC is found by reading the address's ERC-20 transfers
    ("erc20-transfers") and keeping the USDC ones -- or, with
    `usdc_only`, by asking for just its USDC transfers ("usdc-transfers"),
    which needs fewer pages but which for some addresses the explorer
    takes too long over (HTTP 524 after about 2 minutes); then it falls
    back to "erc20-transfers".

    `previous`: a progress record for the check with more pages left
    ("more": query -> next page's position), to carry on from -- or an
    earlier version's record of a limited ERC-20 search ("limited":
    pages), which saved no position, so the address is searched again
    from the start, reading that many pages more than then.

    Returns the transfers found (only the new ones, when carrying on),
    query -> next page's position for the queries with more pages left,
    query -> pages read, and whether this carried on from `previous`."""
    transfers: list[dict] = []
    more: dict[str, dict] = {}
    pages: dict[str, int] = {}

    def read(query: str, limit: int, start: dict | None = None, slow_raises: bool = False) -> None:
        found, next_params, read_pages = read_query(session, address, query, counter, limit, start, slow_raises)
        transfers.extend(found)
        pages[query] = read_pages
        if next_params:
            more[query] = next_params

    if previous and previous.get("more"):
        for query, start in previous["more"].items():
            try:
                read(query, max_pages, start, slow_raises=query == "usdc-transfers")
            except TooSlow as exc:
                raise RuntimeError(f"{exc}: the explorer took too long carrying on with {query}") from None
        return transfers, more, pages, True

    limit = max_pages + (previous.get("limited", 0) if previous else 0)
    if asset == "eth":
        read("transactions", limit)
        read("internal-transactions", limit)
        return transfers, more, pages, False
    if not usdc_only:
        read("erc20-transfers", limit)
        return transfers, more, pages, False
    try:
        read("usdc-transfers", limit, slow_raises=True)
    except TooSlow as exc:
        print(f" ({exc} for USDC only -- searching all ERC-20 transfers instead) ", end="", flush=True)
        transfers.clear()
        pages.clear()
        more.clear()
        read("erc20-transfers", limit)
    return transfers, more, pages, False


def _plain(amount: Decimal) -> str:
    text = f"{amount:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


# --- Collecting -----------------------------------------------------------


class Funding:
    """What one funder sent one address in one asset."""

    def __init__(self) -> None:
        self.transfers = 0
        self.internal = 0
        self.total = 0
        self.first: str | None = None
        self.last: str | None = None

    def add(self, transfer: dict) -> None:
        self.transfers += 1
        self.internal += transfer.get("internal", False)
        self.total += transfer["value"]
        when = transfer.get("time")
        if when:
            self.first = min(self.first or when, when)
            self.last = max(self.last or when, when)

    def to_record(self) -> list:
        return [self.transfers, self.internal, self.total, self.first, self.last]

    @classmethod
    def from_record(cls, record: list) -> "Funding":
        funding = cls()
        funding.transfers, funding.internal, funding.total, funding.first, funding.last = record
        return funding

    def as_json(self, decimals: int, show_internal: bool) -> dict:
        out = {"transfers": self.transfers}
        if show_internal:
            out["internal_transfers"] = self.internal
        out.update({"amount": _plain(Decimal(self.total).scaleb(-decimals)), "first": self.first, "last": self.last})
        return out


def build_output(
    funding: dict, addresses: dict[str, dict], checked: int, failed: list[dict], records: dict[tuple, dict], max_pages: int
) -> dict:
    """funding: funder -> funded address -> asset -> Funding. records:
    (address, asset) -> the check's progress record."""
    funders = []
    for funder, funded in funding.items():
        entries = []
        for address, by_asset in sorted(funded.items()):
            info = addresses[address]
            # What this funder funds the address as: ETH goes to it as a
            # facilitator/server, USDC as a client. `kinds` is everything
            # the address is.
            funded_as = set()
            if "eth" in by_asset:
                funded_as |= info["kinds"] & set(ETH_FUNDED)
            if "usdc" in by_asset:
                funded_as |= info["kinds"] & set(USDC_FUNDED)
            entry = {
                "address": address,
                "funded_as": sorted(funded_as),
                "kinds": sorted(info["kinds"]),
                "names": sorted(info["names"]),
            }
            for asset, decimals in (("eth", ETH_DECIMALS), ("usdc", USDC_DECIMALS)):
                if asset in by_asset:
                    entry[asset] = by_asset[asset].as_json(decimals, show_internal=asset == "eth")
                    if _incomplete(records[(address, asset)]):
                        entry[asset]["more_pages"] = True
            entries.append(entry)
        own = addresses.get(funder)
        funders.append(
            {
                "address": funder,
                "funds": sorted({kind for entry in entries for kind in entry["funded_as"]}),
                "funder_is": sorted(own["kinds"]) if own else [],
                "funder_names": sorted(own["names"]) if own else [],
                "funded_count": len(entries),
                "funded": entries,
            }
        )
    funders.sort(key=lambda f: (-f["funded_count"], f["address"]))
    return {
        "gathered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chain": CHAIN,
        "checks": "ETH sent to facilitator and server addresses; USDC sent to client addresses",
        "addresses_checked": checked,
        "funder_count": len(funders),
        "failed": failed,
        "incomplete": incomplete_json(addresses, records, max_pages),
        "funders": funders,
    }


def _incomplete(record: dict) -> bool:
    """Whether a check stopped with more pages it didn't read (or is an
    earlier version's limited search)."""
    return bool(record.get("more") or record.get("limited"))


def _record_pages(record: dict) -> tuple[dict[str, int], list[str], int]:
    """A record's pages read per query, the queries with more pages, and
    the transfers found -- for an earlier version's limited search too."""
    if record.get("limited"):
        return {"erc20-transfers": record["limited"]}, ["erc20-transfers"], record.get("usdc_transfers", 0)
    return record.get("pages", {}), sorted(record.get("more", {})), record.get("transfers", 0)


def incomplete_json(addresses: dict[str, dict], records: dict[tuple, dict], max_pages: int) -> dict:
    """The checks that stopped with more pages left, so may have missed
    funders."""
    entries = []
    for (address, asset), record in sorted(records.items()):
        if not _incomplete(record):
            continue
        info = addresses[address]
        pages, more, transfers = _record_pages(record)
        entries.append(
            {
                "address": address,
                "asset": asset.upper(),
                "kinds": sorted(info["kinds"]),
                "names": sorted(info["names"]),
                "pages_read": pages,
                "more_pages": more,
                "transfers_found": transfers,
                "no_transfers_found": transfers == 0,
            }
        )
    return {
        "note": (
            f"Each explorer query reads at most {max_pages} page(s) ({ITEMS_PER_PAGE} items a page, newest first) "
            "per address at a time. These checks stopped with more pages left (more_pages: the queries that did), "
            "so funders may be missing; run 07 with --more-pages to read more. pages_read: per query, so far. "
            "no_transfers_found: none was found in the pages read. erc20-transfers: the address's ERC-20 transfers, "
            "searched for USDC; usdc-transfers: its USDC transfers only."
        ),
        "count": len(entries),
        "no_transfers_found_count": sum(1 for e in entries if e["no_transfers_found"]),
        "checks": entries,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find the funders of facilitator, server and client addresses on Base.")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        metavar="N",
        help=f"read at most N pages ({ITEMS_PER_PAGE} items a page) of each explorer query per address "
        f"(default {DEFAULT_MAX_PAGES})",
    )
    parser.add_argument(
        "--usdc-only-query",
        action="store_true",
        help="ask the explorer for just each client address's USDC transfers, rather than reading all its "
        "ERC-20 transfers and keeping the USDC ones; fewer pages, but for some addresses the explorer takes "
        "about 2 minutes to give up on it (then all ERC-20 transfers are read instead)",
    )
    parser.add_argument(
        "--more-pages",
        action="store_true",
        help="for the checks that stopped with more pages left, read up to --max-pages more of each, "
        "carrying on from where they stopped, and rewrite dataset/funders.json",
    )
    args = parser.parse_args()
    if args.max_pages < 1:
        parser.error("--max-pages must be at least 1")
    return args


def main() -> None:
    args = parse_args()
    preflight.check_or_exit(localdb.preflight_checks)
    log = ProgressLog(PROGRESS_NAME)
    if args.more_pages:
        if not log.path.exists():
            print(
                f"nothing to read more of: there's no progress file ({log.path.relative_to(GATHER_ROOT)}), "
                "which is kept after a run only while some checks have more pages left (or failed).",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        exit_if_exists(OUTPUT_PATH)

    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur:
            addresses = fetch_addresses(cur)
    finally:
        conn.close()

    checks = [(address, "eth") for address, info in addresses.items() if info["kinds"] & set(ETH_FUNDED)]
    checks += [(address, "usdc") for address, info in addresses.items() if info["kinds"] & set(USDC_FUNDED)]
    if not checks:
        print("no Ethereum-style facilitator, server or client addresses in the database.", file=sys.stderr)
        sys.exit(2)
    eth_count = sum(1 for _a, asset in checks if asset == "eth")
    print(
        f"checking {eth_count:,} facilitator/server address(es) for ETH and "
        f"{len(checks) - eth_count:,} client address(es) for USDC, on Base, "
        f"reading at most {args.max_pages} page(s) of each query per address"
    )

    # What an earlier run saved, for each check; a check that's had more
    # pages read (--more-pages) has a later record, which wins.
    wanted = set(checks)
    records: dict[tuple, dict] = {}
    for record in log.load():
        key = (record["address"], record["asset"])
        if key in wanted:
            records[key] = record
    if args.more_pages:
        remaining = [check for check in checks if check in records and _incomplete(records[check])]
        if remaining:
            print(f"reading more pages for {len(remaining):,} check(s) that stopped with more left")
        else:
            print("nothing to read more of: no check stopped with more pages left.")
    else:
        remaining = [check for check in checks if check not in records]
        if records:
            print(
                f"resuming: {len(records):,} of {len(checks):,} check(s) already done, saved in "
                f"{log.path.relative_to(GATHER_ROOT)}"
            )

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    errors: dict[tuple, str] = {}
    consecutive_failures = 0
    total = len(checks)
    done = len(records)

    try:
        for n, (address, asset) in enumerate(remaining, 1):
            info = addresses[address]
            position = f"{n}/{len(remaining)}" if args.more_pages else f"{done + n}/{total}"
            print(f"[{position}] {address} {'+'.join(sorted(info['kinds']))} {asset.upper()} ", end="", flush=True)
            previous = records.get((address, asset))
            try:
                received, more, pages, carried_on = check_address(
                    session, address, asset, PageCounter(), args.max_pages, previous, args.usdc_only_query
                )
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                print(f"\n  failed: {exc}", flush=True)
                errors[(address, asset)] = str(exc)
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(
                        f"stopping: {consecutive_failures} failures in a row -- progress is saved; "
                        "run this again to carry on from here.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                continue
            consecutive_failures = 0

            # Carrying on adds to what the earlier pages found; otherwise
            # this replaces any earlier record.
            found: dict[str, Funding] = defaultdict(Funding)
            transfers = len(received)
            if carried_on:
                for funder, saved in previous["funders"].items():
                    found[funder] = Funding.from_record(saved)
                transfers += previous.get("transfers", 0)
                pages = {q: previous.get("pages", {}).get(q, 0) + pages.get(q, 0) for q in {*previous.get("pages", {}), *pages}}
            for transfer in received:
                funder = transfer["from"]
                if not funder or funder in (ZERO_ADDRESS, address):
                    continue
                found[funder].add(transfer)
            record = {
                "address": address,
                "asset": asset,
                "funders": {f: fund.to_record() for f, fund in found.items()},
                "transfers": transfers,
                "pages": pages,
            }
            if more:
                record["more"] = more
            log.append(record)
            records[(address, asset)] = record
            note = f" -- more pages left ({', '.join(sorted(more))})" if more else ""
            so_far = " in all" if carried_on else ""
            print(f" {transfers:,} transfer(s) from {len(found):,} funder(s){so_far}{note}", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted -- progress is saved; run this again to carry on from here.", file=sys.stderr)
        sys.exit(130)

    failed = [
        {"address": a, "asset": asset.upper(), "kinds": sorted(addresses[a]["kinds"]), "error": errors.get((a, asset), "not checked")}
        for a, asset in checks
        if (a, asset) not in records
    ]
    # funder -> funded address -> asset -> Funding
    funding: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(Funding)))
    for (address, asset), record in records.items():
        for funder, saved in record["funders"].items():
            funding[funder][address][asset] = Funding.from_record(saved)
    output = build_output(funding, addresses, total - len(failed), failed, records, args.max_pages)
    write_json(OUTPUT_PATH, output)
    multi = sum(1 for f in output["funders"] if f["funded_count"] > 1)
    print(
        f"found {output['funder_count']:,} funder(s), {multi:,} of them funding more than one address; "
        f"wrote {OUTPUT_PATH.relative_to(GATHER_ROOT)}"
    )
    incomplete = output["incomplete"]["count"]
    if incomplete:
        print(
            f"{incomplete:,} check(s) stopped with more pages left "
            f"({output['incomplete']['no_transfers_found_count']:,} of them having found nothing) -- listed under "
            f"incomplete in {OUTPUT_PATH.name}; run this with --more-pages to read more of them."
        )
    if args.more_pages and errors:
        print(f"{len(errors)} check(s) couldn't be carried on; they're unchanged.", file=sys.stderr)
    if failed:
        print(
            f"{len(failed)} address check(s) failed -- listed in {OUTPUT_PATH.name}. To retry just those, "
            f"delete {OUTPUT_PATH.name} and run this again (progress is kept until no checks have failed "
            "or have more pages left).",
            file=sys.stderr,
        )
    if not failed and not incomplete:
        log.remove()


if __name__ == "__main__":
    main()
