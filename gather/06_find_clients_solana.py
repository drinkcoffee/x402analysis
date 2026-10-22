#!/usr/bin/env python3
"""Finds clients -- accounts that have paid servers in USDC on Solana --
and stores them in gather's database and in dataset/clients_solana.json.
The Solana counterpart of 05_find_clients_base.py.

Uses Solana's standard JSON-RPC API, through Helius
(https://www.helius.dev/; a free key is enough, with the key in
gather/.env as HELIUS_API_KEY); any other provider's Solana endpoint
(Alchemy, QuickNode, Triton, ...) with --rpc-url, its URL in gather/.env
as SOLANA_RPC_URL; or Solana's free public endpoint with --public-rpc (no
key needed, but much more heavily rate limited, so much slower). For every Solana server address (a base58 address decoding to 32
bytes):
  1. getTokenAccountsByOwner finds its USDC token accounts. On Solana,
     tokens aren't held by a wallet address itself but by token accounts it
     owns, and that's where payments arrive. Only real USDC counts (Circle's
     mint, EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v). An account the
     server has since closed isn't found, so payments into it are missed.
  2. getSignaturesForAddress lists every transaction touching each of
     those accounts, newest first, 1,000 a page -- with no cap unless
     --max-transactions is given; failed transactions are skipped.
  3. getTransaction (jsonParsed) fetches each one, and every SPL Token
     transfer / transferChecked instruction into the account -- including
     inner instructions, made by another program -- is a payment. The
     client is the instruction's authority: the wallet that signed the
     transfer. (In x402 on Solana the facilitator pays the fee, so the fee
     payer isn't the client.) Transfers whose authority is the server
     address itself are skipped.

Requests are spread over TX_WORKERS threads, at most --rps a second in all
(default: HELIUS_RPS, or 10 -- Helius's free plan limit -- for Helius;
SOLANA_RPC_RPS, or 10, for --rpc-url; 1 for the public endpoint, which rate limits anything faster so often that less gets done). Rate-limited and failed requests are retried
with a growing wait, saying so. Errors never show the RPC URL, since it
carries the API key.

In the database, as 05 does for Base:
  - each client address is added to addresses (as is -- Solana addresses
    are case-sensitive -- with chains "solana", source "Helius", "Solana public RPC", or --rpc-url's host name), with a
    clients row linked to it through linkaddresses (owner 2, client).
  - each transfer becomes a client_transactions row, chain "solana":
    client address, server, amount (whole USDC, as text), currency
    ("USDC"), transaction signature (tx_hash), time, and -- as log_index --
    the transfer instruction's position in the transaction, counting every
    instruction in the order it ran (each top-level instruction, then the
    inner ones it made). A payment address shared by several servers gets a
    row per server.
dataset/clients_solana.json, written at the end from what's in the
database, has each client's totals, per server, plus any server address
that couldn't be checked.

Resumable, a page at a time: each page of signatures' clients and
transactions is committed to the database as soon as it's read, and where
it got to (each USDC account's last signature read) is noted in
localdb/progress/06_find_clients_solana.jsonl -- the latest line for an
address wins. If a run stops part way -- Ctrl-C, a crash, or five failed
addresses in a row (e.g. an outage, or the plan's credits running out) --
running it again skips the addresses already done and carries on with the
one it was on from the page it had got to. A transaction that's already
stored (for an address with a single USDC account) isn't fetched again.
(Records from before this was paged -- just address and missing -- count as
done.)

--max-transactions N caps how many transactions are read per server
address, newest first, so a few addresses with very many (an exchange's,
say) can't hold up the rest: at about 3 a second on the public endpoint,
2,000 take about 11 minutes. An address stopped at the cap is listed under
"incomplete" in dataset/clients_solana.json, its clients so far counted; a
later run with --more-transactions [--max-transactions N] carries on with
just those, from where each stopped (the next N, or all the rest), and
rewrites the file.

When a run finishes with no failed and no incomplete addresses, the
progress file is removed; otherwise it's kept. Deleting
dataset/clients_solana.json and running this again (without
--more-transactions) retries just the failed ones.

Exits, changing nothing, if dataset/clients_solana.json exists (unless
--more-transactions), or if client_transactions has Solana rows but there's
no progress file to carry on from. Progress, per server address: "[n/total] <address>", how many USDC
accounts it has, a "." per 100 transactions fetched (with a count every
2,000), then that address's totals.

Usage:
    python gather/06_find_clients_solana.py [--public-rpc | --rpc-url] [--rps N] [--max-transactions N]
    python gather/06_find_clients_solana.py --more-transactions [--public-rpc | --rpc-url] [--max-transactions N]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
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

from gatherlib import DATASET_DIR, localdb, preflight, solana  # noqa: E402
from gatherlib.common import (  # noqa: E402
    get_or_create_address,
    link_address,
    exit_if_exists,
    write_json,
)
from gatherlib.progress import ProgressLog  # noqa: E402

OUTPUT_PATH = DATASET_DIR / "clients_solana.json"
OWNER_CLIENT = 2
CURRENCY = "USDC"
CHAIN = "solana"
PROGRESS_NAME = "06_find_clients_solana"

TX_WORKERS = 8
MAX_CONSECUTIVE_FAILURES = 5


def parse_args() -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = argparse.ArgumentParser(description="Find clients that have paid servers in USDC on Solana.")
    solana.add_rpc_arguments(parser)
    parser.add_argument(
        "--max-transactions",
        type=int,
        metavar="N",
        help="read at most N transactions per server address (newest first) this run; the rest can be read later "
        "with --more-transactions (default: no cap)",
    )
    parser.add_argument(
        "--more-transactions",
        action="store_true",
        help="carry on with the addresses an earlier capped run stopped short (the next --max-transactions N, or "
        "all the rest), rewriting dataset/clients_solana.json",
    )
    args = parser.parse_args()
    if args.max_transactions is not None and args.max_transactions < 1:
        parser.error("--max-transactions must be at least 1")
    return parser, args


def check_not_gathered(log: ProgressLog, more: bool) -> None:
    """Exits if this has already been run to the end -- unless an earlier
    run left a progress file to carry on from. With --more-transactions,
    exits unless there's a progress file."""
    if more:
        if not log.path.exists():
            print(
                f"no progress file ({log.path.relative_to(GATHER_ROOT)}) -- nothing for --more-transactions to "
                "carry on with.",
                file=sys.stderr,
            )
            sys.exit(2)
        return
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
    if has_rows and not log.path.exists():
        print(
            "the client_transactions table already has Solana rows, and there's no progress file "
            f"({log.path.relative_to(GATHER_ROOT)}) to carry on from -- nothing done.",
            file=sys.stderr,
        )
        sys.exit(2)


def fetch_server_addresses(cur) -> dict[str, list[tuple[int, str]]]:
    """Solana server address -> [(server id, server name)], sorted by
    address."""
    cur.execute(
        """
        SELECT a.address, s.id, s.name
        FROM linkaddresses l
        JOIN addresses a ON a.id = l.address
        JOIN server s ON s.id = l.ref
        WHERE l.owner = 1 AND a.address !~ '^0x'
        ORDER BY a.address, s.name
        """
    )
    servers: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for address, server_id, name in cur.fetchall():
        if solana.is_solana_address(address):
            servers[address].append((server_id, name))
    return dict(servers)

# --- Finding transfers ----------------------------------------------------


def page_transfers(
    rpc: solana.SolanaRpc, pool: ThreadPoolExecutor, account: str, entries: list[dict], progress: solana.Progress
) -> tuple[list[dict], int]:
    """The USDC transfers into token account `account` in the transactions
    `entries` (a signature page); also how many transactions the RPC listed
    but couldn't return."""
    transfers: list[dict] = []
    missing = 0
    for entry, tx in solana.page_transactions(rpc, pool, entries, progress):
        if tx is None:
            missing += 1
            continue
        block_time = entry.get("blockTime") or tx.get("blockTime")
        for position, instruction, _inner in solana.instructions_in_order(tx):
            transfer = solana.usdc_transfer_into(instruction, account)
            if transfer:
                transfers.append(
                    {
                        "signature": entry["signature"],
                        "position": position,
                        "client": transfer[0],
                        "amount": Decimal(transfer[1]).scaleb(-solana.USDC_DECIMALS),
                        "time": datetime.fromtimestamp(block_time, timezone.utc) if block_time else None,
                    }
                )
    return transfers, missing


def stored_signatures(cur, signatures: list[str], server_ids: list[int]) -> set[str]:
    """Which of `signatures` already have client_transactions rows for any
    of `server_ids` -- so needn't be fetched again."""
    cur.execute(
        "SELECT DISTINCT tx_hash FROM client_transactions WHERE chain = %s AND tx_hash = ANY(%s) AND server = ANY(%s)",
        (CHAIN, signatures, server_ids),
    )
    return {row[0] for row in cur.fetchall()}


def _plain(amount: Decimal) -> str:
    text = f"{amount:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


# --- Storing --------------------------------------------------------------


class ClientStore:
    """Writes clients and their transactions within the caller's
    transaction, creating each client's addresses/clients/linkaddresses
    rows the first time it's seen -- or reusing them if an earlier run
    already did."""

    def __init__(self, cur, source: str) -> None:
        self.cur = cur
        self.source = source
        self.address_ids: dict[str, int] = {}

    def client_address_id(self, address: str) -> int:
        if address not in self.address_ids:
            address_id = get_or_create_address(self.cur, address, self.source)
            self.cur.execute("UPDATE addresses SET chains = %s WHERE id = %s AND chains IS NULL", (CHAIN, address_id))
            self.cur.execute(
                "SELECT 1 FROM linkaddresses WHERE owner = %s AND address = %s LIMIT 1", (OWNER_CLIENT, address_id)
            )
            if self.cur.fetchone() is None:
                self.cur.execute("INSERT INTO clients DEFAULT VALUES RETURNING id")
                link_address(self.cur, OWNER_CLIENT, self.cur.fetchone()[0], address_id)
            self.address_ids[address] = address_id
        return self.address_ids[address]

    def forget(self) -> None:
        """Drops the cached ids, after the transaction they were created in
        was rolled back."""
        self.address_ids.clear()

    def add_transactions(self, rows: list[tuple]) -> int:
        """Inserts `rows`, skipping any already stored; returns how many
        were new."""
        inserted = psycopg2.extras.execute_values(
            self.cur,
            """
            INSERT INTO client_transactions (client, server, amount, currency, chain, tx_hash, log_index, timestamp)
            VALUES %s
            ON CONFLICT (tx_hash, log_index, server) DO NOTHING
            RETURNING 1
            """,
            rows,
            page_size=1000,
            fetch=True,
        )
        return len(inserted)


def client_totals(cur) -> list[dict]:
    """Every Solana client in client_transactions, with its transfers and
    USDC per server (most USDC first), sorted by address."""
    cur.execute(
        """
        SELECT a.address, s.name, count(*), sum(ct.amount::numeric)
        FROM client_transactions ct
        JOIN addresses a ON a.id = ct.client
        JOIN server s ON s.id = ct.server
        WHERE ct.chain = %s
        GROUP BY a.address, s.name
        """,
        (CHAIN,),
    )
    totals: dict[str, list[dict]] = defaultdict(list)
    for client, server, count, amount in cur.fetchall():
        totals[client].append({"server": server, "transfers": count, "usdc": amount})
    clients = []
    for client in sorted(totals):
        servers = sorted(totals[client], key=lambda s: -s["usdc"])
        clients.append({"address": client, "servers": [{**s, "usdc": _plain(s["usdc"])} for s in servers]})
    return clients


def complete(record: dict | None) -> bool:
    """Whether a progress record says its address is done. Records from
    before the cap (just address and missing) always are."""
    return record is not None and record.get("complete", True)


def check_address(
    cur,
    conn,
    store: ClientStore,
    rpc: solana.SolanaRpc,
    pool: ThreadPoolExecutor,
    log: ProgressLog,
    address: str,
    servers: list[tuple[int, str]],
    record: dict | None,
    budget: int | None,
) -> dict:
    """Reads `address`'s USDC transfers a signature page at a time, carrying
    on from `record` (its latest progress record, if any) and stopping after
    `budget` transactions (None: no cap). Each page's transfers are
    committed, and a progress record saying where it got to appended, as
    soon as it's read. Returns the last record."""
    if record and "accounts" in record:
        cursors = dict(record["accounts"])
        finished = set(record.get("finished") or [])
        print(f"{len(cursors)} USDC account(s), carrying on ", end="", flush=True)
    else:
        cursors = {account: None for account in solana.usdc_token_accounts(rpc, address)}
        finished = set()
        print(f"{len(cursors)} USDC account(s) ", end="", flush=True)
    record = {
        "address": address,
        "missing": (record or {}).get("missing", 0),
        "read": (record or {}).get("read", 0),
        "accounts": cursors,
        "finished": sorted(finished),
        "complete": False,
        # Kept while it's being read, so an address interrupted during
        # --more-transactions is still picked up by the next one.
        "more": bool((record or {}).get("more")),
    }
    server_ids = [server_id for server_id, _name in servers]
    # A transaction stored for one account can't be told from one stored
    # for another, so only skip already-stored ones with a single account.
    can_skip = len(cursors) == 1
    progress = solana.Progress()
    read = transfers_found = skipped = 0
    usdc = Decimal(0)
    clients: set[str] = set()
    while True:
        waiting = [account for account in cursors if account not in finished]
        if not waiting or (budget is not None and read >= budget):
            break
        for account in waiting:
            limit = solana.SIGNATURES_PER_PAGE if budget is None else min(solana.SIGNATURES_PER_PAGE, budget - read)
            if limit <= 0:
                break
            entries = solana.signature_page(rpc, account, cursors[account], limit)
            to_fetch = entries
            if can_skip and entries:
                stored = stored_signatures(cur, [entry["signature"] for entry in entries], server_ids)
                to_fetch = [entry for entry in entries if entry["signature"] not in stored]
                skipped += len(entries) - len(to_fetch)
            transfers, missing = page_transfers(rpc, pool, account, to_fetch, progress)
            rows = []
            try:
                for transfer in transfers:
                    client = transfer["client"]
                    if client == address:
                        continue
                    client_id = store.client_address_id(client)
                    clients.add(client)
                    usdc += transfer["amount"]
                    transfers_found += 1
                    for server_id in server_ids:
                        rows.append(
                            (
                                client_id,
                                server_id,
                                _plain(transfer["amount"]),
                                CURRENCY,
                                CHAIN,
                                transfer["signature"],
                                transfer["position"],
                                transfer["time"],
                            )
                        )
                store.add_transactions(rows)
                conn.commit()
            except BaseException:
                conn.rollback()
                store.forget()
                raise
            read += len(entries)
            if entries:
                cursors[account] = entries[-1]["signature"]
            if len(entries) < limit:
                finished.add(account)
            record = {
                **record,
                "missing": record["missing"] + missing,
                "read": record["read"] + len(entries),
                "accounts": dict(cursors),
                "finished": sorted(finished),
            }
            log.append(record)
    record = {**record, "complete": len(finished) == len(cursors)}
    record["more"] = not record["complete"]
    log.append(record)
    shared = f", shared by {len(servers)} servers" if len(servers) > 1 else ""
    not_returned = f", {record['missing']:,} transaction(s) not returned by the RPC" if record["missing"] else ""
    already = f", {skipped:,} already stored" if skipped else ""
    more = f" -- stopped at the cap, {record['read']:,} transactions read so far; more with --more-transactions" if record["more"] else ""
    print(
        f"\n  {read:,} transaction(s) read{already}: {transfers_found:,} transfer(s) from {len(clients):,} client(s), "
        f"{_plain(usdc)} USDC{shared}{not_returned}{more}",
        flush=True,
    )
    return record


def incomplete_json(records: dict[str, dict], server_addresses: dict[str, list[tuple[int, str]]]) -> dict:
    """The output's "incomplete" section: addresses a capped run stopped
    short."""
    short = [record for record in records.values() if record.get("more")]
    return {
        "note": "These server addresses' transactions were read newest first and stopped at --max-transactions, so "
        "older transfers (and clients) are missing; run 06 with --more-transactions to read more. "
        "transactions_read: so far.",
        "count": len(short),
        "addresses": [
            {
                "address": record["address"],
                "servers": [name for _id, name in server_addresses.get(record["address"], [])],
                "transactions_read": record.get("read", 0),
            }
            for record in sorted(short, key=lambda record: record["address"])
        ],
    }


def main() -> None:
    parser, args = parse_args()
    rpc, rpc_name, rpc_hint = solana.rpc_from_args(parser, args)
    log = ProgressLog(PROGRESS_NAME)
    preflight.check_or_exit(localdb.preflight_checks)
    check_not_gathered(log, args.more_transactions)

    solana.check_answering(rpc, rpc_name + rpc_hint)

    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur:
            server_addresses = fetch_server_addresses(cur)
    finally:
        conn.close()
    if not server_addresses:
        print("no Solana server addresses in the database -- run 02_services.py first.", file=sys.stderr)
        sys.exit(2)

    # The latest record for each address wins.
    records: dict[str, dict] = {}
    for record in log.load():
        if record["address"] in server_addresses:
            records[record["address"]] = record
    total = len(server_addresses)
    done = sum(1 for record in records.values() if complete(record))
    capped = sum(1 for record in records.values() if record.get("more"))
    if args.more_transactions:
        todo = {a: s for a, s in server_addresses.items() if records.get(a, {}).get("more")}
    else:
        todo = {a: s for a, s in server_addresses.items() if not complete(records.get(a)) and not records.get(a, {}).get("more")}
    cap = f", at most {args.max_transactions:,} transactions per address" if args.max_transactions else ""
    print(f"checking {total:,} server address(es) for USDC transfers on Solana, through {rpc_name}{cap}")
    if records:
        print(
            f"resuming: {done:,} of {total:,} done, {capped:,} stopped at a cap, saved in {log.path.relative_to(GATHER_ROOT)}"
        )
    if args.more_transactions:
        print(f"reading more for {len(todo):,} address(es) stopped at a cap")

    failed: list[dict] = []
    consecutive_failures = 0
    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur, ThreadPoolExecutor(max_workers=TX_WORKERS) as pool:
            store = ClientStore(cur, rpc_name)
            for n, (address, servers) in enumerate(todo.items(), 1):
                print(f"[{n}/{len(todo)}] {address} ", end="", flush=True)
                record = records.get(address)
                budget = args.max_transactions
                if budget is not None and not args.more_transactions and record:
                    budget = max(0, budget - record.get("read", 0))  # an interrupted capped address
                try:
                    records[address] = check_address(cur, conn, store, rpc, pool, log, address, servers, record, budget)
                except solana.UsageExhausted as exc:
                    print(
                        f"\n{exc} -- stopping. Progress is saved; run this again to carry on once the "
                        "credits have reset (or with --rpc-url or --public-rpc).",
                        file=sys.stderr,
                    )
                    os._exit(1)  # don't wait for the pool's queued requests
                except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
                    print(f"\n  failed: {exc}", flush=True)
                    failed.append({"address": address, "servers": [name for _id, name in servers], "error": str(exc)})
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        print(
                            f"stopping: {consecutive_failures} failures in a row -- progress is saved; "
                            "run this again to carry on from here.",
                            file=sys.stderr,
                        )
                        os._exit(1)  # don't wait for the pool's queued requests
                    continue
                consecutive_failures = 0
            clients_out = client_totals(cur)
    except KeyboardInterrupt:
        print("\ninterrupted -- progress is saved; run this again to carry on from here.", file=sys.stderr)
        os._exit(130)  # don't wait for the pool's queued requests
    finally:
        conn.close()

    # Addresses not checked: failed this run, or (with --more-transactions)
    # not finished from before.
    if args.more_transactions:
        previous = json.loads(OUTPUT_PATH.read_text()) if OUTPUT_PATH.exists() else {}
        failed_now = {entry["address"] for entry in failed}
        failed += [entry for entry in previous.get("failed") or [] if entry["address"] not in failed_now and not complete(records.get(entry["address"]))]
    incomplete = incomplete_json(records, server_addresses)
    missing_total = sum(record.get("missing", 0) for record in records.values())
    write_json(
        OUTPUT_PATH,
        {
            "gathered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "chain": CHAIN,
            "currency": CURRENCY,
            "rpc": rpc_name,
            "server_addresses_checked": total - len(failed),
            "transactions_not_returned": missing_total,
            "failed": failed,
            "incomplete": incomplete,
            "clients": clients_out,
        },
    )
    print(f"found {len(clients_out):,} client(s) in all; wrote {OUTPUT_PATH.relative_to(GATHER_ROOT)}")
    if incomplete["count"]:
        print(
            f"{incomplete['count']:,} address(es) stopped at the cap -- listed under incomplete; run with "
            "--more-transactions to read more.",
            file=sys.stderr,
        )
    if failed:
        print(
            f"{len(failed)} server address(es) couldn't be checked -- listed in {OUTPUT_PATH.name}. To retry just "
            f"those, delete {OUTPUT_PATH.name} and run this again (progress is kept until a run has no failures).",
            file=sys.stderr,
        )
    if not failed and not incomplete["count"]:
        log.remove()


if __name__ == "__main__":
    main()
