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
    contract on Base; GET .../token-transfers?filter=to&token=<USDC>).
An address that's more than one kind -- e.g. a server that's also a
client -- gets every check that applies. Every page is fetched, with no
cap. Senders that are the zero address (ETH or USDC created out of
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
many addresses they fund, most first. Server/client addresses that
couldn't be checked are listed too.

Doesn't change the database. Exits, doing nothing, if
dataset/funders.json exists.

Resumable: as each address is checked, what was found is saved to
localdb/progress/07_find_funders_base.jsonl, so a run that stops part way
-- Ctrl-C, a crash, or five failed addresses in a row -- carries on from
there when it's run again. An explorer outage first pauses the run:
server errors, timeouts and dropped connections are retried for about 9
minutes before an address counts as failed. dataset/funders.json is
written once every address has been checked; the progress file is then
deleted, unless some addresses failed -- then it's kept, so deleting
funders.json and running again retries just those.

Progress, per address checked: "[n/total] <address> <kinds> <asset>", a
"." per page of transfers (with a page count every 20 pages), then what
was found.

Usage:
    python gather/07_find_funders_base.py
"""

from __future__ import annotations

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
TIMEOUT = 30.0
RETRY_WAITS = (15, 30, 60, 120, 300)  # seconds -- about 9 minutes in all
MAX_RATE_LIMIT_WAITS = 30
DEFAULT_RETRY_WAIT = 60.0
PAGE_COUNT_EVERY = 20
MAX_CONSECUTIVE_FAILURES = 5


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


def get_page(session: requests.Session, url: str, params: dict, referer: str) -> dict:
    """One page of results. Waits out the rate limit, and retries a server
    error (5xx), timeout or dropped connection after each of RETRY_WAITS --
    so an explorer outage of up to about 9 minutes pauses the run rather
    than failing the address."""
    failures = rate_limited = 0
    while True:
        try:
            response = session.get(url, params=params, timeout=TIMEOUT, headers={"Referer": referer})
        except (requests.Timeout, requests.ConnectionError) as exc:
            problem = type(exc).__name__
        else:
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


def fetch_items(session: requests.Session, address: str, path: str, params: dict, tab: str, counter: PageCounter):
    """Every item from one of the address's list endpoints, across all
    pages."""
    url = ADDRESS_API.format(address=address, path=path)
    referer = f"{EXPLORER}/address/{address}?tab={tab}"
    page_params = dict(params)
    while True:
        page = get_page(session, url, page_params, referer)
        counter.tick()
        yield from page.get("items") or []
        next_params = page.get("next_page_params")
        if not next_params:
            return
        page_params = {**next_params, **params}


def _hash(value) -> str:
    """An address from a Blockscout field: either a plain string or an
    object with a "hash"."""
    if isinstance(value, dict):
        value = value.get("hash")
    return (value or "").lower()


def eth_received(session: requests.Session, address: str, counter: PageCounter) -> list[dict]:
    """Every ETH transfer to `address`: {"from", "value" (wei), "time",
    "internal"}."""
    received = []
    for item in fetch_items(session, address, "transactions", {"filter": "to"}, "txs", counter):
        if item.get("status") == "ok" and _hash(item.get("to")) == address and int(item.get("value") or 0) > 0:
            received.append({"from": _hash(item.get("from")), "value": int(item["value"]), "time": item.get("timestamp"), "internal": False})
    for item in fetch_items(session, address, "internal-transactions", {"filter": "to"}, "internal_txns", counter):
        if (
            item.get("success") is True
            and item.get("type") in VALUE_CALL_TYPES
            and _hash(item.get("to")) == address
            and int(item.get("value") or 0) > 0
        ):
            received.append({"from": _hash(item.get("from")), "value": int(item["value"]), "time": item.get("timestamp"), "internal": True})
    return received


def usdc_received(session: requests.Session, address: str, counter: PageCounter) -> list[dict]:
    """Every real USDC transfer to `address`: {"from", "value" (smallest
    units), "time"}."""
    received = []
    params = {"filter": "to", "token": USDC}
    for item in fetch_items(session, address, "token-transfers", params, "token_transfers", counter):
        # The API filters by token already; checked again, so nothing but
        # real USDC is ever counted.
        if _hash((item.get("token") or {}).get("address_hash")) != USDC or _hash(item.get("to")) != address:
            continue
        value = int((item.get("total") or {}).get("value") or 0)
        if value > 0:
            received.append({"from": _hash(item.get("from")), "value": value, "time": item.get("timestamp")})
    return received


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


def build_output(funding: dict, addresses: dict[str, dict], checked: int, failed: list[dict]) -> dict:
    """funding: funder -> funded address -> asset -> Funding."""
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
            if "eth" in by_asset:
                entry["eth"] = by_asset["eth"].as_json(ETH_DECIMALS, show_internal=True)
            if "usdc" in by_asset:
                entry["usdc"] = by_asset["usdc"].as_json(USDC_DECIMALS, show_internal=False)
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
        "funders": funders,
    }


def main() -> None:
    preflight.check_or_exit(localdb.preflight_checks)
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
        f"{len(checks) - eth_count:,} client address(es) for USDC, on Base"
    )

    # funder -> funded address -> asset -> Funding, starting from whatever
    # an earlier, unfinished run saved.
    funding: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(Funding)))
    log = ProgressLog(PROGRESS_NAME)
    wanted = set(checks)
    done = 0
    for record in log.load():
        key = (record["address"], record["asset"])
        if key in wanted:
            wanted.discard(key)
            done += 1
            for funder, saved in record["funders"].items():
                funding[funder][record["address"]][record["asset"]] = Funding.from_record(saved)
    remaining = [check for check in checks if check in wanted]
    if done:
        print(f"resuming: {done:,} of {len(checks):,} check(s) already done, saved in {log.path.relative_to(GATHER_ROOT)}")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    failed: list[dict] = []
    consecutive_failures = 0
    total = len(checks)

    try:
        for n, (address, asset) in enumerate(remaining, done + 1):
            info = addresses[address]
            print(f"[{n}/{total}] {address} {'+'.join(sorted(info['kinds']))} {asset.upper()} ", end="", flush=True)
            counter = PageCounter()
            try:
                received = (eth_received if asset == "eth" else usdc_received)(session, address, counter)
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                print(f"\n  failed: {exc}", flush=True)
                failed.append({"address": address, "asset": asset.upper(), "kinds": sorted(info["kinds"]), "error": str(exc)})
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

            found: dict[str, Funding] = defaultdict(Funding)
            for transfer in received:
                funder = transfer["from"]
                if not funder or funder in (ZERO_ADDRESS, address):
                    continue
                found[funder].add(transfer)
            log.append({"address": address, "asset": asset, "funders": {f: fund.to_record() for f, fund in found.items()}})
            for funder, fund in found.items():
                funding[funder][address][asset] = fund
            print(f" {len(received):,} transfer(s) from {len(found):,} funder(s)", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted -- progress is saved; run this again to carry on from here.", file=sys.stderr)
        sys.exit(130)

    output = build_output(funding, addresses, total - len(failed), failed)
    write_json(OUTPUT_PATH, output)
    multi = sum(1 for f in output["funders"] if f["funded_count"] > 1)
    print(
        f"found {output['funder_count']:,} funder(s), {multi:,} of them funding more than one address; "
        f"wrote {OUTPUT_PATH.relative_to(GATHER_ROOT)}"
    )
    if failed:
        print(
            f"{len(failed)} address check(s) failed -- listed in {OUTPUT_PATH.name}. To retry just those, "
            f"delete {OUTPUT_PATH.name} and run this again (progress is kept until a run has no failures).",
            file=sys.stderr,
        )
    else:
        log.remove()


if __name__ == "__main__":
    main()
