#!/usr/bin/env python3
"""Ranks servers' payment addresses by how much USDC they've received, from
the transfers 05_find_clients_base.py and 06_find_clients_solana.py stored
in gather's database (client_transactions).

client_transactions has a row per transfer per server, so a payment
address shared by several servers has each of its transfers once per
server; summing by server counts them several times over. This counts
each transfer (chain, tx_hash, log_index) once, under its payment address:
the address whose linked servers are exactly the servers the transfer was
credited to. (Where several addresses are linked to exactly the same
servers -- a server with two payment addresses, say -- they can't be told
apart, so they're ranked together and all listed.)

For each payment address (or group), prints:
  - USDC received, transfers, and distinct clients (paying addresses).
  - the median transfer, in USDC (below 0.01, to as many places as it
    takes to show it): a few large transfers (top-ups, moves
    between an operator's own wallets) give a high total from a high
    median; x402 usage is many small payments, a low median.
  - how much of it came from phishing: USDC paid by clients that are
    themselves marked as phishing, or that a funder marked as phishing
    funded (gatherlib.phishing: "phish" in their Blockscout name or tags,
    from dataset/metadata_base_phishing_only_annotated.json; funders from dataset/funders.json or
    its pieces) -- for an address annotated with a "from" time (when it
    became phishing), only its payments from then on, and only funding it
    sent from then on, with the clients it funded counted from then on
    too -- ignoring a funder's sends worth less than US$0.01 in all
    (--min-funding; ETH at US$2,500, --eth-usd) as dust -- tiny sends are
    what address poisoning looks like, so they mark a target, not someone
    the phishing address funded. Both are Base data, so it's 0 for Solana
    addresses.
    Then that as a percentage of the USDC received.
  - the chain, the address(es), and the servers linked to them (the first
    few, then how many more).
  - the categories of the services those servers offer, from
    dataset/services-annotated.json (matched by server name): unique,
    alphabetical, across all the servers, as annotated.
  - the countries the servers' API URLs geolocate to (as in
    31_countries_servers.py; CDNs such as Cloudflare geolocate to the
    CDN), from the same file: unique, alphabetical.
  - whether the payment address was funded by a funder marked as
    phishing (one of the addresses those funders funded, as in
    35_malicious_analysis.py, dust ignored): "yes" or "no" -- "n/a" for Solana
    addresses, since funders were only gathered on Base.
Sorted by USDC received, most first; the top 20 by default (--top N, 0 for
all).

By default every transfer counts. A window limits that to transfers in it
-- everything above but the phishing-funded column, since funders were
gathered without one:
  --months N          the N months up to each chain's latest transfer
                      gathered (Base's and Solana's were gathered at
                      different times, so counting back from today would
                      leave a gap with no data for one of them).
  --since DATE        from DATE (YYYY-MM-DD, UTC) on.
  --since DATE --months N
                      the N months starting on DATE.
The window used, per chain, goes to standard error.

Every row -- whatever --top says -- is also written to a CSV file in
dataset/ (overwritten each run), named for the window and chain:
  phishing_funds_flow.csv                    all time, both chains
  phishing_funds_flow-3m.csv                 --months 3
  phishing_funds_flow-since-2026-07-01.csv   --since 2026-07-01
  phishing_funds_flow-2026-07-01-3m.csv      --since 2026-07-01 --months 3
  ...-base.csv / ...-solana.csv              with --chain
with plain numbers (USDC to 6 places, no thousands separators) and all
the servers, columns: rank, usdc_received, transfers, clients,
median_transfer, from_phishing, from_phishing_percent (to 2 places),
chain, country, phishing_funded, payment_addresses, servers, categories
(the lists "; "-separated).
Any USDC sent to the address counts, whatever it was for. Addresses
05 couldn't check (dataset/clients.json's "failed") aren't in the database,
so aren't ranked; how many there are goes to standard error.

Reads the database; changes nothing there. Can be run any number of times.

Usage:
    python gather/36_server_usdc_received.py [--top N] [--chain base|solana]
                                             [--months N] [--since YYYY-MM-DD]
                                             [--min-funding USD] [--eth-usd USD]
"""

from __future__ import annotations

import argparse
import csv
from datetime import date
import json
import os
import re
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

GATHER_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(GATHER_ROOT))

from gatherlib import DATASET_DIR, localdb  # noqa: E402
from gatherlib.countries import country  # noqa: E402
from gatherlib.phishing import add_dust_arguments, earliest, phishing_and_funded  # noqa: E402

CLIENTS_PATH = DATASET_DIR / "clients.json"
SERVICES_PATH = DATASET_DIR / "services-annotated.json"
CSV_STEM = "phishing_funds_flow"
CSV_COLUMNS = [
    "rank",
    "usdc_received",
    "transfers",
    "clients",
    "median_transfer",
    "from_phishing",
    "from_phishing_percent",
    "chain",
    "country",
    "phishing_funded",
    "payment_addresses",
    "servers",
    "categories",
]
CHAINS = ("base", "solana")
DEFAULT_TOP = 20
SERVERS_SHOWN = 3
BASE_ADDRESS = re.compile(r"0x[0-9a-f]{40}")
SOLANA_ADDRESS = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")

# Each transfer once (credited to every server its address is linked to),
# then totals per (chain, set of servers); the last, of those paid by the
# client address ids given ({id: the time from which its payments count,
# or "-infinity"}, as JSON). Only transfers in each chain's window ({chain:
# start} and {chain: end}, as JSON; a chain not in one isn't bounded).
RECEIVED_SQL = """
    WITH transfers AS (
        SELECT chain, tx_hash, log_index, min(amount)::numeric AS amount, min(client) AS client,
               min(timestamp) AS timestamp,
               array_agg(server ORDER BY server) AS servers
        FROM client_transactions
        WHERE currency = 'USDC' AND chain = ANY(%s)
          AND timestamp >= coalesce((%s::jsonb ->> chain)::timestamptz, '-infinity')
          AND timestamp < coalesce((%s::jsonb ->> chain)::timestamptz, 'infinity')
        GROUP BY chain, tx_hash, log_index
    )
    SELECT chain, servers, sum(amount), count(*), count(DISTINCT client),
           percentile_cont(0.5) WITHIN GROUP (ORDER BY amount),
           coalesce(sum(amount) FILTER (WHERE timestamp >= (%s::jsonb ->> client::text)::timestamptz), 0)
    FROM transfers
    GROUP BY chain, servers
"""

# Each server payment address with the servers linked to it.
ADDRESSES_SQL = """
    SELECT a.address, array_agg(s.id ORDER BY s.id)
    FROM linkaddresses l
    JOIN addresses a ON a.id = l.address
    JOIN server s ON s.id = l.ref
    WHERE l.owner = 1
    GROUP BY a.address
"""


def normalise(address: str) -> str:
    """Ethereum-style addresses in lower case; any other as it is."""
    return address.lower() if address[:2].lower() == "0x" else address


def chain_of(address: str) -> str | None:
    """"base" for an Ethereum-style address, "solana" for a Solana one,
    None for anything else (servers list a few other kinds)."""
    if BASE_ADDRESS.fullmatch(address):
        return "base"
    if SOLANA_ADDRESS.fullmatch(address):
        return "solana"
    return None


def usdc(amount, places: int = 2) -> str:
    """`amount` to `places` decimal places, with thousands separators."""
    return f"{Decimal(str(amount)).quantize(Decimal(1).scaleb(-places)):,}"


def median_text(amount) -> str:
    """A median, to 2 places -- or, below 0.01, to as many (up to USDC's
    6) as it takes to show it, so dust payments don't show as 0.00."""
    value = Decimal(str(amount))
    if value == 0 or value >= Decimal("0.01"):
        return usdc(value)
    return f"{value.quantize(Decimal('0.000001')).normalize():f}"


def server_info() -> dict[str, tuple[set[str], str]]:
    """Server name -> (the categories of its services, the country its
    API URL geolocates to), from SERVICES_PATH ({} if it isn't there)."""
    if not SERVICES_PATH.exists():
        print(f"{SERVICES_PATH.relative_to(GATHER_ROOT)} doesn't exist -- no categories or countries shown.", file=sys.stderr)
        return {}
    return {
        server["name"]: (
            {service.get("category") or "uncategorised" for service in server.get("services") or []},
            country(server, "api_url"),
        )
        for server in json.loads(SERVICES_PATH.read_text())["servers"]
    }


def percent(part, whole) -> Decimal:
    """`part` as a percentage of `whole` (0 if `whole` is)."""
    whole = Decimal(str(whole))
    return Decimal(str(part)) * 100 / whole if whole else Decimal(0)


def phishing_funded(chain: str, payment_addresses: list[str], funded: dict) -> str:
    if chain != "base":
        return "n/a"
    return "yes" if any(address in funded for address in payment_addresses) else "no"


def plain(amount) -> str:
    """`amount` to USDC's 6 places, with no thousands separators."""
    return f"{Decimal(str(amount)).quantize(Decimal('0.000001'))}"


def csv_path(months: int | None, since: date | None, chain: str | None) -> Path:
    """The CSV file for this window and chain (see the module docstring)."""
    name = CSV_STEM
    if since and months:
        name += f"-{since.isoformat()}-{months}m"
    elif since:
        name += f"-since-{since.isoformat()}"
    elif months:
        name += f"-{months}m"
    if chain:
        name += f"-{chain}"
    return DATASET_DIR / f"{name}.csv"


def windows(cur, chains: list[str], months: int | None, since: date | None) -> tuple[dict, dict]:
    """({chain: window start}, {chain: window end}) as ISO timestamps, for
    the chains bounded at that end (see the module docstring)."""
    if not months and not since:
        return {}, {}
    if since:
        start = f"{since.isoformat()}T00:00:00+00:00"
        if not months:
            return {chain: start for chain in chains}, {}
        cur.execute("SELECT %s::timestamptz + make_interval(months => %s)", (start, months))
        end = cur.fetchone()[0].isoformat()
        return {chain: start for chain in chains}, {chain: end for chain in chains}
    cur.execute(
        """
        SELECT chain, max(timestamp) - make_interval(months => %s)
        FROM client_transactions
        WHERE currency = 'USDC' AND chain = ANY(%s)
        GROUP BY chain
        """,
        (months, chains),
    )
    return {chain: start.isoformat() for chain, start in cur.fetchall()}, {}


def latest(cur, chains: list[str]) -> dict[str, str]:
    """Each chain's latest transfer gathered, as a date."""
    cur.execute(
        "SELECT chain, max(timestamp)::date FROM client_transactions WHERE currency = 'USDC' AND chain = ANY(%s) GROUP BY chain",
        (chains,),
    )
    return {chain: day.isoformat() for chain, day in cur.fetchall()}


def write_csv(path: Path, records: list[dict]) -> None:
    """Writes `records` to `path` via a temporary file renamed into place,
    so it's never left half-written."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(records)
    os.replace(tmp, path)


def servers_text(names: list[str]) -> str:
    shown = "; ".join(names[:SERVERS_SHOWN])
    return shown + (f" (+{len(names) - SERVERS_SHOWN} more)" if len(names) > SERVERS_SHOWN else "")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help=f"how many to show (default {DEFAULT_TOP}; 0: all)")
    parser.add_argument("--chain", choices=CHAINS, help="only this chain (default: both)")
    parser.add_argument("--months", type=int, help="only the N months up to each chain's latest transfer (or from --since)")
    parser.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD", help="only transfers from this date (UTC) on")
    add_dust_arguments(parser)
    args = parser.parse_args()
    if args.months is not None and args.months < 1:
        parser.error("--months must be at least 1")
    chains = [args.chain] if args.chain else list(CHAINS)
    output = csv_path(args.months, args.since, args.chain)
    phishing, funded = phishing_and_funded(args.min_funding, args.eth_usd)
    info = server_info()

    with localdb.connect_existing() as conn, conn.cursor() as cur:
        # Windows' dates and month arithmetic in UTC, whatever the server's
        # time zone.
        cur.execute("SET TIME ZONE 'UTC'")
        since = dict(funded)
        for address, moment in phishing.items():
            since[address] = earliest(since[address], moment) if address in since else moment
        cur.execute("SELECT id, lower(address) FROM addresses WHERE lower(address) = ANY(%s)", (sorted(since),))
        phishing_ids = {
            str(address_id): since[address].isoformat() if since[address] else "-infinity"
            for address_id, address in cur.fetchall()
        }
        cur.execute("SELECT id, name FROM server")
        names = dict(cur.fetchall())
        cur.execute(ADDRESSES_SQL)
        addresses: dict[tuple[str, tuple[int, ...]], list[str]] = defaultdict(list)
        for address, server_ids in cur.fetchall():
            address = normalise(address)
            if chain_of(address):
                addresses[(chain_of(address), tuple(server_ids))].append(address)
        starts, ends = windows(cur, chains, args.months, args.since)
        ends_at = latest(cur, chains)
        cur.execute(RECEIVED_SQL, (chains, json.dumps(starts), json.dumps(ends), json.dumps(phishing_ids)))
        received = cur.fetchall()
    for chain in chains:
        if chain in ends_at:
            start = starts.get(chain, "")[:10] or "the first transfer"
            end = ends.get(chain, "")[:10]
            span = f"up to (not including) {end}" if end else f"to the latest transfer gathered, {ends_at[chain]}"
            print(f"{chain}: transfers from {start} {span}", file=sys.stderr)
    if not received:
        print("None")
        return

    received.sort(key=lambda r: -r[2])
    total = sum(r[2] for r in received)
    from_phishing = sum(r[6] for r in received)
    with_phishing = sum(1 for r in received if r[6])
    records = []
    for rank, (chain, server_ids, amount, transfers, clients, median, phished) in enumerate(received, start=1):
        server_names = sorted(names[i] for i in server_ids)
        payment = sorted(addresses.get((chain, tuple(server_ids)), ["?"]))
        records.append(
            {
                "rank": rank,
                "usdc_received": amount,
                "transfers": transfers,
                "clients": clients,
                "median_transfer": median,
                "from_phishing": phished,
                "from_phishing_percent": percent(phished, amount),
                "chain": chain,
                "country": sorted({info[name][1] for name in server_names if name in info}),
                "phishing_funded": phishing_funded(chain, payment, funded),
                "payment_addresses": payment,
                "servers": server_names,
                "categories": sorted(set().union(*(info[name][0] for name in server_names if name in info))),
            }
        )
    write_csv(
        output,
        [
            {
                **r,
                **{key: plain(r[key]) for key in ("usdc_received", "median_transfer", "from_phishing")},
                "from_phishing_percent": f"{r['from_phishing_percent']:.2f}",
                **{key: "; ".join(r[key]) for key in ("country", "payment_addresses", "servers", "categories")},
            }
            for r in records
        ]
    )
    rows = [
        [
            str(r["rank"]),
            usdc(r["usdc_received"]),
            f"{r['transfers']:,}",
            f"{r['clients']:,}",
            median_text(r["median_transfer"]),
            median_text(r["from_phishing"]),
            f"{r['from_phishing_percent']:.1f}%",
            r["chain"],
            ", ".join(r["country"]),
            r["phishing_funded"],
            ", ".join(r["payment_addresses"]),
            servers_text(r["servers"]),
            ", ".join(r["categories"]),
        ]
        for r in records[: args.top or None]
    ]
    header = ["#", "USDC received", "Transfers", "Clients", "Median", "From phishing", "% phishing", "Chain", "Country", "Phishing-funded", "Payment address", "Servers", "Categories"]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header) - 1)]
    right = {0, 1, 2, 3, 4, 5, 6}
    for r in [header, ["-" * w for w in widths] + ["-" * len(header[-1])], *rows]:
        cells = [c.rjust(w) if i in right else c.ljust(w) for i, (c, w) in enumerate(zip(r, widths))]
        print("  ".join([*cells, r[-1]]))

    failed = 0
    if "base" in chains and CLIENTS_PATH.exists():
        failed = len(json.loads(CLIENTS_PATH.read_text()).get("failed") or [])
    print(
        f"{len(received):,} payment address(es) (or groups) on {', '.join(chains)} received {usdc(total)} USDC; "
        f"{usdc(from_phishing)} of it from {len(phishing):,} phishing address(es) and the {len(funded):,} they "
        f"funded with at least US${args.min_funding} (ETH at US${args.eth_usd}), paid to {with_phishing:,} payment address(es); showing {len(rows):,}, "
        f"all {len(records):,} written to {output.relative_to(GATHER_ROOT)}"
        + (f". {failed:,} Base server address(es) couldn't be checked by 05 and aren't ranked" if failed else ""),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
