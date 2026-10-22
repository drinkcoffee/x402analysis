#!/usr/bin/env python3
"""Finds clients -- accounts that have paid servers in USDC on Base -- and
stores them in gather's database and in dataset/clients.json.

For every Ethereum-style address linked to a server, fetches every USDC
transfer to it from Blockscout's Base explorer API (the same API, and the
same approach, as x402-analysis-api/scripts/l_find_users_scrape_blockscout.py):

    GET https://base.blockscout.com/api/v2/addresses/<address>/token-transfers
        ?filter=to&token=<USDC contract>

Only real USDC counts (Circle's contract on Base,
0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913). Every page is fetched, with
no cap. The API sits behind a Cloudflare bot check that blocks plain
scripted requests, so requests send the browser-like headers the explorer
page does; it's also rate limited, and when the limit's reached this waits
for it to reset and carries on (saying so).

Each sender is a client, except the zero address (USDC minted straight to
the server address) and the server address itself. In the database:
  - each client address is added to addresses (lower case, chains
    "base", source "Blockscout"), with a clients row linked to it through
    linkaddresses (owner 2, client).
  - each transfer becomes a client_transactions row: client address,
    server, amount (whole USDC, as text), currency ("USDC"), transaction
    hash, log index and time. A payment address shared by several servers
    gets a row per server.
dataset/clients.json has each client's totals, per server, plus any
server address that couldn't be checked.

Exits, changing nothing, if dataset/clients.json exists or
client_transactions has Base rows. Everything is written in one transaction at
the end, so an interrupted run (Ctrl-C) -- or one stopped by five failed
addresses in a row -- changes nothing. Progress, per server address:
"[n/total] <address>", a "." per page of transfers (with a page count
every 20 pages), then that address's totals.

--retry-failed checks again just the server addresses dataset/clients.json
lists as failed. Those usually failed because the USDC-only query above
timed out (it's slow for some addresses), so the retry asks for all the
address's ERC-20 transfers instead and keeps the USDC ones:

    GET .../addresses/<address>/token-transfers?filter=to&type=ERC-20

-- the same transfers, more pages. Timeouts and 5xx answers are retried
(RETRY_WAITS) before an address counts as failed. Each address's clients
and transfers are committed as soon as it's done, so an interrupted retry
keeps what it finished; transfers already stored are skipped, and a sender
that's already a client isn't added again. dataset/clients.json is then
rebuilt from the database's Base transfers (which gives exactly what the
first run wrote, plus the new ones), with only the addresses still not
checked left under "failed" -- also on Ctrl-C, or when five addresses in a
row fail. It can be run again to retry those.

Usage:
    python gather/05_find_clients_base.py
    python gather/05_find_clients_base.py --retry-failed
"""

from __future__ import annotations

import argparse
import json
import re
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
    import psycopg2.extras
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
from gatherlib.common import (  # noqa: E402
    get_or_create_address,
    link_address,
    exit_if_exists,
    write_json,
)

OUTPUT_PATH = DATASET_DIR / "clients.json"
OWNER_CLIENT = 2
CURRENCY = "USDC"
SOURCE = "Blockscout"
CHAIN = "base"

EXPLORER = "https://base.blockscout.com"
TRANSFERS_URL = EXPLORER + "/api/v2/addresses/{address}/token-transfers"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDC_DECIMALS = 6
ZERO_ADDRESS = "0x" + "0" * 40
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130 Safari/537.36"
)
TIMEOUT = 30.0
RETRY_TIMEOUT = 60.0
# --retry-failed: seconds to wait before each retry of a timed-out or 5xx
# page; after the last, the address has failed.
RETRY_WAITS = (15, 30, 60, 120)
MAX_RETRIES = 5
DEFAULT_RETRY_WAIT = 60.0
PAGE_COUNT_EVERY = 20
MAX_CONSECUTIVE_FAILURES = 5


def check_not_gathered() -> None:
    exit_if_exists(OUTPUT_PATH)
    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur:
            localdb.ensure_schema(cur)
            cur.execute("SELECT EXISTS (SELECT 1 FROM client_transactions WHERE chain = %s)", (CHAIN,))
            has_rows = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    if has_rows:
        print("the client_transactions table already has Base rows -- nothing done.", file=sys.stderr)
        sys.exit(2)


def fetch_server_addresses(cur) -> dict[str, list[tuple[int, str]]]:
    """Ethereum-style server address -> [(server id, server name)], sorted
    by address."""
    cur.execute(
        """
        SELECT a.address, s.id, s.name
        FROM linkaddresses l
        JOIN addresses a ON a.id = l.address
        JOIN server s ON s.id = l.ref
        WHERE l.owner = 1 AND a.address ~ '^0x[0-9a-fA-F]{40}$'
        ORDER BY a.address, s.name
        """
    )
    servers: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for address, server_id, name in cur.fetchall():
        servers[address.lower()].append((server_id, name))
    return dict(sorted(servers.items()))


# --- Blockscout ----------------------------------------------------------
# Adapted from x402-analysis-api/scripts/l_find_users_scrape_blockscout.py.


def _reset_wait(response: requests.Response) -> float:
    """Seconds until the rate limit resets (x-ratelimit-reset is in ms)."""
    try:
        return int(response.headers["x-ratelimit-reset"]) / 1000 + 1
    except (KeyError, ValueError):
        return DEFAULT_RETRY_WAIT


class ServerError(RuntimeError):
    """A 5xx answer from the explorer (e.g. Cloudflare's 524, a timeout
    behind it)."""


def get_page(session: requests.Session, url: str, params: dict, timeout: float = TIMEOUT) -> dict:
    """One page of transfers, waiting out the rate limit if needed."""
    for _attempt in range(MAX_RETRIES):
        response = session.get(url, params=params, timeout=timeout)
        if response.status_code == 429:
            wait = _reset_wait(response)
            print(f"\nrate limited -- waiting {wait:.0f}s", flush=True)
            time.sleep(wait)
            continue
        if response.status_code == 403 and "cloudflare" in response.headers.get("server", ""):
            raise RuntimeError("HTTP 403 from Cloudflare's bot check -- the explorer is refusing scripted requests")
        if response.status_code >= 500:
            raise ServerError(f"HTTP {response.status_code}")
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
        if response.headers.get("x-ratelimit-remaining") == "0":
            wait = _reset_wait(response)
            print(f"\nrate limit reached -- waiting {wait:.0f}s", flush=True)
            time.sleep(wait)
        return response.json()
    raise RuntimeError(f"still rate limited after {MAX_RETRIES} attempts")


def get_page_patiently(session: requests.Session, url: str, params: dict) -> dict:
    """get_page, retrying timeouts and 5xx answers after RETRY_WAITS."""
    for wait in (*RETRY_WAITS, None):
        try:
            return get_page(session, url, params, timeout=RETRY_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError, ServerError) as exc:
            if wait is None:
                raise RuntimeError(f"{exc} -- still, after {len(RETRY_WAITS)} retries") from exc
            print(f"\n  {exc} -- retrying in {wait}s ", end="", flush=True)
            time.sleep(wait)
    raise AssertionError("unreachable")


def fetch_transfers(address: str, all_erc20: bool = False) -> list[dict]:
    """Every USDC transfer to `address`, across all pages, printing a "."
    per page (a page count every PAGE_COUNT_EVERY pages). `all_erc20`: ask
    for every ERC-20 transfer to it (and keep the USDC ones) rather than
    just USDC's, retrying slow pages -- for --retry-failed."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Referer": f"{EXPLORER}/address/{address}?tab=token_transfers",
        }
    )
    url = TRANSFERS_URL.format(address=address)
    query = {"filter": "to", "type": "ERC-20"} if all_erc20 else {"filter": "to", "token": USDC}
    params: dict = dict(query)
    transfers: list[dict] = []
    page_number = 0
    while True:
        page = get_page_patiently(session, url, params) if all_erc20 else get_page(session, url, params)
        # The API filters by token already; checked again, so nothing but
        # real USDC is ever counted.
        transfers.extend(
            item for item in page.get("items") or [] if ((item.get("token") or {}).get("address_hash") or "").lower() == USDC
        )
        page_number += 1
        print(f"({page_number} pages)" if page_number % PAGE_COUNT_EVERY == 0 else ".", end="", flush=True)
        next_params = page.get("next_page_params")
        if not next_params:
            return transfers
        params = {**next_params, **query}


def amount_of(transfer: dict) -> Decimal:
    """The transfer's amount in whole USDC."""
    return Decimal((transfer.get("total") or {}).get("value") or 0).scaleb(-USDC_DECIMALS)


def _plain(amount: Decimal) -> str:
    text = f"{amount:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


# --- Storing --------------------------------------------------------------


class ClientStore:
    """Writes clients and their transactions within the caller's
    transaction, creating each client's addresses/clients/linkaddresses
    rows the first time it's seen."""

    def __init__(self, cur) -> None:
        self.cur = cur
        self.address_ids: dict[str, int] = {}

    def client_address_id(self, address: str) -> int:
        """The address id of client `address`, adding it (and a clients
        row linked to it) if it isn't a client already."""
        if address not in self.address_ids:
            address_id = get_or_create_address(self.cur, address, SOURCE)
            self.cur.execute("UPDATE addresses SET chains = %s WHERE id = %s AND chains IS NULL", (CHAIN, address_id))
            self.cur.execute(
                "SELECT EXISTS (SELECT 1 FROM linkaddresses WHERE owner = %s AND address = %s)", (OWNER_CLIENT, address_id)
            )
            if not self.cur.fetchone()[0]:
                self.cur.execute("INSERT INTO clients DEFAULT VALUES RETURNING id")
                link_address(self.cur, OWNER_CLIENT, self.cur.fetchone()[0], address_id)
            self.address_ids[address] = address_id
        return self.address_ids[address]

    def add_transactions(self, rows: list[tuple]) -> None:
        psycopg2.extras.execute_values(
            self.cur,
            """
            INSERT INTO client_transactions (client, server, amount, currency, chain, tx_hash, log_index, timestamp)
            VALUES %s
            ON CONFLICT (tx_hash, log_index, server) DO NOTHING
            """,
            rows,
            page_size=1000,
        )


def store_transfers(store: ClientStore, address: str, servers, transfers: list[dict], totals=None):
    """Stores `transfers` to server address `address` (linked to `servers`,
    [(id, name)]) as client_transactions rows, adding to `totals` (client
    -> server name -> [transfers, USDC]) if given. Returns (rows stored,
    the senders, USDC)."""
    rows = []
    senders: set[str] = set()
    usdc = Decimal(0)
    for transfer in transfers:
        sender = ((transfer.get("from") or {}).get("hash") or "").lower()
        if not sender or sender in (ZERO_ADDRESS, address):
            continue
        amount = amount_of(transfer)
        client_id = store.client_address_id(sender)
        senders.add(sender)
        usdc += amount
        for server_id, name in servers:
            rows.append(
                (
                    client_id,
                    server_id,
                    _plain(amount),
                    CURRENCY,
                    CHAIN,
                    transfer.get("transaction_hash"),
                    transfer.get("log_index"),
                    transfer.get("timestamp"),
                )
            )
            if totals is not None:
                entry = totals[sender][name]
                entry[0] += 1
                entry[1] += amount
    store.add_transactions(rows)
    return len(rows), senders, usdc


def write_output(clients: list[dict], checked: int, failed: list[dict]) -> None:
    write_json(
        OUTPUT_PATH,
        {
            "gathered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "chain": CHAIN,
            "currency": CURRENCY,
            "server_addresses_checked": checked,
            "failed": failed,
            "clients": clients,
        },
    )


def clients_from_db(cur) -> list[dict]:
    """Each client's totals per server, from the database's Base USDC
    transfers -- as main() builds them from what it fetched."""
    cur.execute(
        """
        SELECT lower(a.address), s.name, count(*), sum(ct.amount::numeric)
        FROM client_transactions ct
        JOIN addresses a ON a.id = ct.client
        JOIN server s ON s.id = ct.server
        WHERE ct.chain = %s AND ct.currency = %s
        GROUP BY 1, 2
        """,
        (CHAIN, CURRENCY),
    )
    totals: dict[str, dict[str, tuple[int, Decimal]]] = defaultdict(dict)
    for client, name, count, amount in cur.fetchall():
        totals[client][name] = (count, Decimal(amount))
    return [
        {
            "address": client,
            "servers": [
                {"server": name, "transfers": count, "usdc": _plain(amount)}
                for name, (count, amount) in sorted(by_server.items(), key=lambda item: (-item[1][1], item[0]))
            ],
        }
        for client, by_server in sorted(totals.items())
    ]


def retry_failed() -> None:
    """--retry-failed: see the module docstring."""
    if not OUTPUT_PATH.exists():
        print(f"{OUTPUT_PATH.relative_to(GATHER_ROOT)} doesn't exist -- run without --retry-failed first.", file=sys.stderr)
        sys.exit(2)
    previous = json.loads(OUTPUT_PATH.read_text())
    to_retry = [entry["address"].lower() for entry in previous.get("failed") or []]
    if not to_retry:
        print("no failed server addresses to retry.")
        return

    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur:
            server_addresses = fetch_server_addresses(cur)
        conn.commit()
        total = len(to_retry)
        print(f"retrying {total:,} failed server address(es), reading all their ERC-20 transfers")
        still_failed: list[dict] = []
        done: set[str] = set()
        consecutive_failures = 0
        transaction_rows = 0
        stopped = None
        for n, address in enumerate(to_retry, 1):
            servers = server_addresses.get(address)
            if not servers:
                print(f"[{n}/{total}] {address}: no longer linked to a server -- dropped")
                done.add(address)
                continue
            print(f"[{n}/{total}] {address} ", end="", flush=True)
            try:
                transfers = fetch_transfers(address, all_erc20=True)
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                print(f"\n  failed: {exc}")
                still_failed.append({"address": address, "servers": [name for _id, name in servers], "error": str(exc)})
                done.add(address)
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    stopped = f"stopping: {consecutive_failures} failures in a row"
                    break
                continue
            except KeyboardInterrupt:
                stopped = "interrupted"
                break
            consecutive_failures = 0
            with conn.cursor() as cur:
                rows, senders, usdc = store_transfers(ClientStore(cur), address, servers, transfers)
            conn.commit()
            done.add(address)
            transaction_rows += rows
            shared = f", shared by {len(servers)} servers" if len(servers) > 1 else ""
            print(f"\n  {len(transfers):,} transfer(s) from {len(senders):,} client(s), {_plain(usdc)} USDC{shared}")

        # Not reached (interrupted or stopped): still failed, as before.
        previous_by_address = {entry["address"].lower(): entry for entry in previous["failed"]}
        still_failed += [previous_by_address[address] for address in to_retry if address not in done]
        with conn.cursor() as cur:
            clients = clients_from_db(cur)
        conn.commit()
    finally:
        conn.close()
    checked = len(server_addresses) - len(still_failed)
    write_output(clients, checked, still_failed)
    if stopped:
        print(f"\n{stopped} -- kept what was finished", file=sys.stderr)
    print(
        f"{total - len(still_failed):,} of {total:,} address(es) checked; stored {transaction_rows:,} client transaction "
        f"row(s); {len(clients):,} client(s) in all; rewrote {OUTPUT_PATH.relative_to(GATHER_ROOT)}"
    )
    if still_failed:
        print(f"{len(still_failed)} server address(es) still couldn't be checked -- listed in {OUTPUT_PATH.name}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="check again just the server addresses dataset/clients.json lists as failed",
    )
    args = parser.parse_args()
    preflight.check_or_exit(localdb.preflight_checks)
    if args.retry_failed:
        retry_failed()
        return
    check_not_gathered()

    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur:
            server_addresses = fetch_server_addresses(cur)
    finally:
        conn.close()
    if not server_addresses:
        print("no Ethereum-style server addresses in the database -- run 02_services.py first.", file=sys.stderr)
        sys.exit(2)

    total = len(server_addresses)
    print(f"checking {total:,} server address(es) for USDC transfers on Base")

    # client -> server name -> [transfers, USDC]
    totals: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(lambda: [0, Decimal(0)]))
    failed: list[dict] = []
    transaction_rows = 0
    consecutive_failures = 0

    # One transaction for the whole run, committed only at the end.
    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur:
            store = ClientStore(cur)
            for n, (address, servers) in enumerate(server_addresses.items(), 1):
                print(f"[{n}/{total}] {address} ", end="", flush=True)
                try:
                    transfers = fetch_transfers(address)
                except (requests.RequestException, RuntimeError, ValueError) as exc:
                    print(f"\n  failed: {exc}")
                    failed.append({"address": address, "servers": [name for _id, name in servers], "error": str(exc)})
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        print(f"stopping: {consecutive_failures} failures in a row -- nothing written", file=sys.stderr)
                        sys.exit(1)
                    continue
                consecutive_failures = 0

                rows, senders, usdc = store_transfers(store, address, servers, transfers, totals)
                transaction_rows += rows
                shared = f", shared by {len(servers)} servers" if len(servers) > 1 else ""
                print(f"\n  {len(transfers):,} transfer(s) from {len(senders):,} client(s), {_plain(usdc)} USDC{shared}")
        conn.commit()
    except KeyboardInterrupt:
        print("\ninterrupted -- nothing written", file=sys.stderr)
        sys.exit(130)
    finally:
        conn.close()

    clients = []
    for client, by_server in sorted(totals.items()):
        servers_out = [
            {"server": name, "transfers": count, "usdc": _plain(amount)}
            for name, (count, amount) in sorted(by_server.items(), key=lambda item: (-item[1][1], item[0]))
        ]
        clients.append({"address": client, "servers": servers_out})
    write_output(clients, total - len(failed), failed)
    print(
        f"found {len(clients):,} client(s); stored {transaction_rows:,} client transaction row(s); "
        f"wrote {OUTPUT_PATH.relative_to(GATHER_ROOT)}"
    )
    if failed:
        print(f"{len(failed)} server address(es) couldn't be checked -- listed in {OUTPUT_PATH.name}", file=sys.stderr)


if __name__ == "__main__":
    main()
