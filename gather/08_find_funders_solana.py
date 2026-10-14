#!/usr/bin/env python3
"""Finds funders on Solana -- the addresses that have sent facilitators
and servers SOL (for fees and rent), and clients USDC (to pay with) -- and
writes them to dataset/funders_solana.json, grouped by funding address,
since one address can fund several. The Solana counterpart of
07_find_funders_base.py.

Uses Solana's standard JSON-RPC API -- through Helius by default (key in
gather/.env as HELIUS_API_KEY); any other provider's Solana endpoint
(Alchemy, QuickNode, Triton, ...) with --rpc-url, its URL in gather/.env
as SOLANA_RPC_URL; or Solana's free public endpoint with --public-rpc (no
key needed, but much more heavily rate limited, so much slower). For every Solana address (base58, decoding to 32 bytes):
  - facilitators and servers, for SOL sent to the address: every
    transaction touching it is fetched (getSignaturesForAddress, then
    getTransaction), and each System program transfer, transferWithSeed,
    createAccount or createAccountWithSeed sending SOL to it counts --
    whether a top-level instruction or one made by another program (an
    "internal" transfer, e.g. from an exchange's or bridge's program). The
    funder is the account the SOL came from.
  - clients, for real USDC sent to them: as 06_find_clients_solana.py
    does, every transaction touching each of the client's USDC token
    accounts (tokens on Solana are held in token accounts a wallet owns),
    and each transfer of USDC into one counts. The funder is the wallet
    that signed the transfer. An account the client has since closed isn't
    found, so transfers into it are missed.
Every transaction is fetched, with no cap -- a facilitator, which signs
every payment it settles, can have millions, so this can take a very long
time and use a lot of Helius credits. Failed transactions don't count, nor
do transfers from the address to itself. An address that's more than one
kind -- e.g. a server that's also a client -- gets every check that
applies. Solana client addresses come from 06_find_clients_solana.py,
which has to have finished first.

Requests are spread over TX_WORKERS threads, at most --rps a second in all
(default: HELIUS_RPS, or 10, for Helius; SOLANA_RPC_RPS, or 10, for
--rpc-url; 3 for the public endpoint).
Rate-limited and failed requests are retried with a growing wait, saying
so.

dataset/funders_solana.json lists each funder: what kinds of address it
funds (facilitator, server and/or client -- SOL funds an address as a
facilitator/server, USDC as a client), what it's known as itself if it's
one of those, and each address it funds -- what it funds it as, all the
kinds it is, its facilitator/server names, and the SOL and/or USDC sent:
number of transfers (for SOL, how many were internal), total amount, and
the first and last transfer's time. Funders are sorted by how many
addresses they fund, most first. Addresses that couldn't be checked are
listed too.

Doesn't change the database. Exits, doing nothing, if
dataset/funders_solana.json exists.

Resumable: as each address is checked, what was found is saved to
localdb/progress/08_find_funders_solana.jsonl, so a run that stops part
way -- Ctrl-C, a crash, or five failed addresses in a row (e.g. once
Helius's credits run out) -- carries on from there when it's run again.
An outage first pauses the run: rate limits, server errors, timeouts and
dropped connections are retried for about 8 minutes before an address
counts as failed. An address stopped part way is checked again from the
start. dataset/funders_solana.json is written once every address has been
checked; the progress file is then deleted, unless some addresses failed
-- then it's kept, so deleting funders_solana.json and running again
retries just those.

Progress, per address checked: "[n/total] <address> <kinds> <asset>", a
"." per 100 transactions fetched (with a count every 2,000), then what
was found.

Usage:
    python gather/08_find_funders_solana.py [--public-rpc | --rpc-url] [--rps N]
"""

from __future__ import annotations

import argparse
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
from gatherlib.common import exit_if_exists, write_json  # noqa: E402
from gatherlib.progress import ProgressLog  # noqa: E402

OUTPUT_PATH = DATASET_DIR / "funders_solana.json"
PROGRESS_NAME = "08_find_funders_solana"
CHAIN = "solana"
KINDS = {0: "facilitator", 1: "server", 2: "client"}
SOL_FUNDED = ("facilitator", "server")
USDC_FUNDED = ("client",)
TX_WORKERS = 8
MAX_CONSECUTIVE_FAILURES = 5


def parse_args() -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = argparse.ArgumentParser(description="Find who funds facilitators, servers and clients on Solana.")
    solana.add_rpc_arguments(parser)
    return parser, parser.parse_args()


def fetch_addresses(cur) -> dict[str, dict]:
    """Solana address -> {"kinds": {kind, ...}, "names": {name, ...}} for
    every facilitator, server and client address, sorted by address."""
    cur.execute(
        """
        SELECT a.address, l.owner, coalesce(f.name, s.name)
        FROM linkaddresses l
        JOIN addresses a ON a.id = l.address
        LEFT JOIN facilitator f ON l.owner = 0 AND f.id = l.ref
        LEFT JOIN server s ON l.owner = 1 AND s.id = l.ref
        WHERE l.owner IN (0, 1, 2) AND a.address !~ '^0x'
        """
    )
    addresses: dict[str, dict] = defaultdict(lambda: {"kinds": set(), "names": set()})
    for address, owner, name in cur.fetchall():
        if not solana.is_solana_address(address):
            continue
        addresses[address]["kinds"].add(KINDS[owner])
        if name:
            addresses[address]["names"].add(name)
    return dict(sorted(addresses.items()))


def _time(entry: dict, tx: dict) -> str | None:
    block_time = entry.get("blockTime") or tx.get("blockTime")
    return datetime.fromtimestamp(block_time, timezone.utc).isoformat() if block_time else None


def sol_received(rpc: solana.SolanaRpc, pool: ThreadPoolExecutor, address: str) -> tuple[list[dict], int]:
    """Every SOL transfer to `address` ({"from", "value" (lamports),
    "time", "internal"}), and how many transactions the RPC listed but
    couldn't return."""
    received: list[dict] = []
    missing = 0
    for entry, tx in solana.transactions(rpc, pool, address, solana.Progress()):
        if tx is None:
            missing += 1
            continue
        for _position, instruction, inner in solana.instructions_in_order(tx):
            transfer = solana.sol_transfer_into(instruction, address)
            if transfer:
                received.append({"from": transfer[0], "value": transfer[1], "time": _time(entry, tx), "internal": inner})
    return received, missing


def usdc_received(rpc: solana.SolanaRpc, pool: ThreadPoolExecutor, address: str) -> tuple[list[dict], int]:
    """Every USDC transfer into any of `address`'s USDC token accounts
    ({"from", "value" (smallest units), "time"}), and how many transactions
    the RPC listed but couldn't return."""
    accounts = solana.usdc_token_accounts(rpc, address)
    print(f"{len(accounts)} USDC account(s) ", end="", flush=True)
    received: list[dict] = []
    missing = 0
    progress = solana.Progress()
    for account in accounts:
        for entry, tx in solana.transactions(rpc, pool, account, progress):
            if tx is None:
                missing += 1
                continue
            for _position, instruction, _inner in solana.instructions_in_order(tx):
                transfer = solana.usdc_transfer_into(instruction, account)
                if transfer:
                    received.append({"from": transfer[0], "value": transfer[1], "time": _time(entry, tx)})
    return received, missing


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


def build_output(funding: dict, addresses: dict[str, dict], checked: int, failed: list[dict], missing: int, rpc_name: str) -> dict:
    """funding: funder -> funded address -> asset -> Funding."""
    funders = []
    for funder, funded in funding.items():
        entries = []
        for address, by_asset in sorted(funded.items()):
            info = addresses[address]
            # What this funder funds the address as: SOL goes to it as a
            # facilitator/server, USDC as a client. `kinds` is everything
            # the address is.
            funded_as = set()
            if "sol" in by_asset:
                funded_as |= info["kinds"] & set(SOL_FUNDED)
            if "usdc" in by_asset:
                funded_as |= info["kinds"] & set(USDC_FUNDED)
            entry = {
                "address": address,
                "funded_as": sorted(funded_as),
                "kinds": sorted(info["kinds"]),
                "names": sorted(info["names"]),
            }
            if "sol" in by_asset:
                entry["sol"] = by_asset["sol"].as_json(solana.SOL_DECIMALS, show_internal=True)
            if "usdc" in by_asset:
                entry["usdc"] = by_asset["usdc"].as_json(solana.USDC_DECIMALS, show_internal=False)
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
        "rpc": rpc_name,
        "checks": "SOL sent to facilitator and server addresses; USDC sent to client addresses",
        "addresses_checked": checked,
        "transactions_not_returned": missing,
        "funder_count": len(funders),
        "failed": failed,
        "funders": funders,
    }


def main() -> None:
    rpc, rpc_name, rpc_hint = solana.rpc_from_args(*parse_args())
    preflight.check_or_exit(localdb.preflight_checks)
    exit_if_exists(OUTPUT_PATH)

    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur:
            addresses = fetch_addresses(cur)
    finally:
        conn.close()

    checks = [(address, "sol") for address, info in addresses.items() if info["kinds"] & set(SOL_FUNDED)]
    sol_count = len(checks)
    checks += [(address, "usdc") for address, info in addresses.items() if info["kinds"] & set(USDC_FUNDED)]
    if len(checks) == sol_count:
        print(
            "no Solana client addresses in the database -- run 06_find_clients_solana.py first, "
            "and let it finish.",
            file=sys.stderr,
        )
        sys.exit(2)
    solana.check_answering(rpc, rpc_name + rpc_hint)
    print(
        f"checking {sol_count:,} facilitator/server address(es) for SOL and "
        f"{len(checks) - sol_count:,} client address(es) for USDC, on Solana, through {rpc_name}"
    )

    # funder -> funded address -> asset -> Funding, starting from whatever
    # an earlier, unfinished run saved.
    funding: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(Funding)))
    log = ProgressLog(PROGRESS_NAME)
    wanted = set(checks)
    done = 0
    missing_total = 0
    for record in log.load():
        key = (record["address"], record["asset"])
        if key in wanted:
            wanted.discard(key)
            done += 1
            missing_total += record.get("missing", 0)
            for funder, saved in record["funders"].items():
                funding[funder][record["address"]][record["asset"]] = Funding.from_record(saved)
    remaining = [check for check in checks if check in wanted]
    if done:
        print(f"resuming: {done:,} of {len(checks):,} check(s) already done, saved in {log.path.relative_to(GATHER_ROOT)}")

    failed: list[dict] = []
    consecutive_failures = 0
    total = len(checks)

    try:
        with ThreadPoolExecutor(max_workers=TX_WORKERS) as pool:
            for n, (address, asset) in enumerate(remaining, done + 1):
                info = addresses[address]
                print(f"[{n}/{total}] {address} {'+'.join(sorted(info['kinds']))} {asset.upper()} ", end="", flush=True)
                try:
                    received, missing = (sol_received if asset == "sol" else usdc_received)(rpc, pool, address)
                except solana.UsageExhausted as exc:
                    print(
                        f"\n{exc} -- stopping. Progress is saved; run this again to carry on once the "
                        "credits have reset (or with --rpc-url or --public-rpc).",
                        file=sys.stderr,
                    )
                    os._exit(1)  # don't wait for the pool's queued requests
                except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
                    print(f"\n  failed: {exc}", flush=True)
                    failed.append({"address": address, "asset": asset.upper(), "kinds": sorted(info["kinds"]), "error": str(exc)})
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
                missing_total += missing

                found: dict[str, Funding] = defaultdict(Funding)
                for transfer in received:
                    if transfer["from"] != address:
                        found[transfer["from"]].add(transfer)
                log.append(
                    {
                        "address": address,
                        "asset": asset,
                        "missing": missing,
                        "funders": {f: fund.to_record() for f, fund in found.items()},
                    }
                )
                for funder, fund in found.items():
                    funding[funder][address][asset] = fund
                not_returned = f", {missing:,} transaction(s) not returned by the RPC" if missing else ""
                print(f" {len(received):,} transfer(s) from {len(found):,} funder(s){not_returned}", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted -- progress is saved; run this again to carry on from here.", file=sys.stderr)
        os._exit(130)  # don't wait for the pool's queued requests

    output = build_output(funding, addresses, total - len(failed), failed, missing_total, rpc_name)
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
