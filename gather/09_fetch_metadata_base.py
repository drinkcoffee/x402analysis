#!/usr/bin/env python3
"""Fetches what's publicly known about every facilitator, server, client
and funder address on Base -- the name, tags and warning notes an explorer
shows beside it -- and stores it in gather's database (address_metadata)
and in dataset/metadata_base.json.

Uses Blockscout's public metadata service, which supplies the tags shown
on Blockscout's explorer pages, as
x402-analysis-api/scripts/l_bc_address_screen_scrape_blockscout_all.py
does, but for Base (chain id 8453) rather than Ethereum:

    GET https://metadata.services.blockscout.com/api/v1/metadata
        ?addresses=<address,address,...>&chainId=8453

It needs no API key; addresses are looked up BATCH_SIZE at a time.

Addresses checked, all Ethereum-style (lower case):
  - every facilitator, server and client address in the database
    (01_facilitators.py, 02_services.py, 05_find_clients_base.py).
  - every funder in dataset/funders.json (07_find_funders_base.py, which
    has to have finished). Funders aren't otherwise in the database, so
    any that aren't are added to addresses (source "07 funders", chains
    "base"); they aren't linked to anything in linkaddresses, since there's
    no funder table to link them to.

For each address with metadata: `name` is its name tag(s), `tags` its
other tags (generic, protocol, ...), in the order the explorer shows them,
and `notes` its warning notes, written "<alert status>: <text>"; several
of any are joined with "; ". Those addresses get an address_metadata row
(chain "base"), and are listed first in the JSON file, with which of
facilitator/server/client/funder they are; addresses with none follow, in
its "no_metadata" list. Addresses whose lookup failed are listed in
"failed".

Exits, changing nothing, if dataset/metadata_base.json exists or
address_metadata has Base rows. Everything is written in one transaction
at the end, so an interrupted run (Ctrl-C) -- or one stopped by five
failed batches in a row -- changes nothing. Progress: a "." per batch,
with a count every 20 batches.

Usage:
    python gather/09_fetch_metadata_base.py
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
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
from gatherlib.common import ETH_ADDRESS, exit_if_exists, get_or_create_address, write_json  # noqa: E402

OUTPUT_PATH = DATASET_DIR / "metadata_base.json"
FUNDERS_PATH = DATASET_DIR / "funders.json"
CHAIN = "base"
CHAIN_ID = 8453
FUNDER_SOURCE = "07 funders"
KINDS = {0: "facilitator", 1: "server", 2: "client"}
KIND_ORDER = ("facilitator", "server", "client", "funder")

METADATA_URL = "https://metadata.services.blockscout.com/api/v1/metadata"
BATCH_SIZE = 100
BATCH_PAUSE = 0.5
TIMEOUT = 30.0
MAX_RETRIES = 5
RETRY_WAIT = 15.0
COUNT_EVERY = 20
MAX_CONSECUTIVE_FAILURES = 5
SEPARATOR = "; "


def check_not_gathered() -> None:
    exit_if_exists(OUTPUT_PATH)
    if not FUNDERS_PATH.exists():
        print(
            f"{FUNDERS_PATH.relative_to(GATHER_ROOT)} doesn't exist -- run 07_find_funders_base.py first, "
            "and let it finish.",
            file=sys.stderr,
        )
        sys.exit(2)
    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur:
            localdb.ensure_schema(cur)
            cur.execute("SELECT EXISTS (SELECT 1 FROM address_metadata WHERE chain = %s)", (CHAIN,))
            has_rows = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    if has_rows:
        print("the address_metadata table already has Base rows -- nothing done.", file=sys.stderr)
        sys.exit(2)


def gather_addresses(cur) -> dict[str, set[str]]:
    """Lower-case address -> the kinds it is, for every Ethereum-style
    facilitator, server and client address in the database and every
    funder in dataset/funders.json; sorted by address."""
    kinds: dict[str, set[str]] = defaultdict(set)
    cur.execute(
        """
        SELECT lower(a.address), l.owner
        FROM linkaddresses l
        JOIN addresses a ON a.id = l.address
        WHERE l.owner IN (0, 1, 2) AND a.address ~ '^0x[0-9a-fA-F]{40}$'
        """
    )
    for address, owner in cur.fetchall():
        kinds[address].add(KINDS[owner])
    for funder in json.loads(FUNDERS_PATH.read_text()).get("funders") or []:
        address = (funder.get("address") or "").lower()
        if ETH_ADDRESS.match(address):
            kinds[address].add("funder")
    return dict(sorted(kinds.items()))


def _ordered(kinds: set[str]) -> list[str]:
    return [kind for kind in KIND_ORDER if kind in kinds]


# --- Blockscout metadata --------------------------------------------------
# Adapted from x402-analysis-api/scripts/l_bc_address_screen_scrape_blockscout.py.


def fetch_batch(session: requests.Session, addresses: list[str]) -> dict[str, list[dict]]:
    """Lower-case address -> its tags, for those of `addresses` that have
    any, retrying rate limits, server errors and network failures."""
    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(
                METADATA_URL,
                params={"addresses": ",".join(addresses), "chainId": CHAIN_ID},
                timeout=TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            problem = type(exc).__name__
        else:
            if response.ok:
                # Keyed by the address in checksum case, whatever case was
                # asked for; addresses with no tags are left out.
                entries = response.json().get("addresses") or {}
                return {address.lower(): entry.get("tags") or [] for address, entry in entries.items()}
            if response.status_code != 429 and response.status_code < 500:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
            problem = f"HTTP {response.status_code}"
        if attempt < MAX_RETRIES - 1:
            wait = RETRY_WAIT * (attempt + 1)
            print(f"\n{problem} -- retrying in {wait:.0f}s", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"still failing after {MAX_RETRIES} attempts ({problem})")


def _meta(tag: dict) -> dict:
    """A tag's `meta`, which comes as a JSON-encoded string."""
    try:
        meta = json.loads(tag.get("meta") or "{}")
    except ValueError:
        return {}
    return meta if isinstance(meta, dict) else {}


def summarize(tags: list[dict]) -> dict[str, str | None]:
    """{"name", "tags", "notes"} from an address's tags, each "; "-joined,
    None if there are none -- name tags, other tags in the order the
    explorer shows them, and notes as "<alert status>: <text>"."""
    ordered = sorted(tags, key=lambda t: -(t.get("ordinal") or 0))
    names = [t.get("name") for t in ordered if t.get("tagType") == "name" and t.get("name")]
    others = [t.get("name") for t in ordered if t.get("tagType") not in ("name", "note") and t.get("name")]
    notes = []
    for tag in ordered:
        if tag.get("tagType") != "note":
            continue
        meta = _meta(tag)
        text = meta.get("data") or tag.get("name")
        status = meta.get("alertStatus")
        if text:
            notes.append(f"{status}: {text}" if status else text)

    def joined(values: list[str]) -> str | None:
        return SEPARATOR.join(dict.fromkeys(values)) or None  # dict.fromkeys drops repeats, keeping order

    return {"name": joined(names), "tags": joined(others), "notes": joined(notes)}


# --- Storing --------------------------------------------------------------


def store(cur, results: dict[str, dict], kinds: dict[str, set[str]]) -> int:
    """Adds funder addresses missing from addresses, then an
    address_metadata row per address with metadata; returns how many
    funders were added."""
    funders = [address for address, k in kinds.items() if "funder" in k]
    cur.execute("SELECT lower(address), id FROM addresses WHERE lower(address) = ANY(%s)", (list(kinds),))
    address_ids = dict(cur.fetchall())
    added = 0
    for address in funders:
        if address not in address_ids:
            address_ids[address] = get_or_create_address(cur, address, FUNDER_SOURCE)
            cur.execute("UPDATE addresses SET chains = %s WHERE id = %s AND chains IS NULL", (CHAIN, address_ids[address]))
            added += 1
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO address_metadata (address, chain, kinds, name, tags, notes)
        VALUES %s
        """,
        [
            (address_ids[address], CHAIN, ", ".join(_ordered(kinds[address])), m["name"], m["tags"], m["notes"])
            for address, m in results.items()
        ],
        page_size=1000,
    )
    return added


def main() -> None:
    preflight.check_or_exit(localdb.preflight_checks)
    check_not_gathered()

    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur:
            kinds = gather_addresses(cur)
    finally:
        conn.close()

    addresses = list(kinds)
    batches = [addresses[i : i + BATCH_SIZE] for i in range(0, len(addresses), BATCH_SIZE)]
    counts = {kind: sum(1 for k in kinds.values() if kind in k) for kind in KIND_ORDER}
    print(
        f"looking up {len(addresses):,} address(es) on Base ("
        + ", ".join(f"{n:,} {kind}" for kind, n in counts.items())
        + f") in {len(batches):,} batch(es)"
    )

    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    results: dict[str, dict] = {}
    failed: list[str] = []
    consecutive_failures = 0
    try:
        for n, batch in enumerate(batches, 1):
            try:
                tags_by_address = fetch_batch(session, batch)
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                print(f"\n  batch {n} failed: {exc}", flush=True)
                failed.extend(batch)
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f"stopping: {consecutive_failures} failures in a row -- nothing written", file=sys.stderr)
                    sys.exit(1)
                continue
            consecutive_failures = 0
            for address, tags in tags_by_address.items():
                if address in kinds:
                    summary = summarize(tags)
                    if any(summary.values()):
                        results[address] = summary
            if n % COUNT_EVERY == 0 or n == len(batches):
                print(f"({n:,}/{len(batches):,} batches, {len(results):,} with metadata)", end="", flush=True)
            else:
                print(".", end="", flush=True)
            time.sleep(BATCH_PAUSE)
        print()

        conn = localdb.connect_existing()
        try:
            with conn.cursor() as cur:
                added = store(cur, results, kinds)
            conn.commit()
        finally:
            conn.close()
    except KeyboardInterrupt:
        print("\ninterrupted -- nothing written", file=sys.stderr)
        sys.exit(130)

    failed_set = set(failed)
    write_json(
        OUTPUT_PATH,
        {
            "gathered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "chain": CHAIN,
            "chain_id": CHAIN_ID,
            "source": METADATA_URL,
            "addresses_checked": len(addresses) - len(failed_set),
            "with_metadata": len(results),
            "addresses": [
                {"address": address, "kinds": _ordered(kinds[address]), **m} for address, m in results.items()
            ],
            "failed": [{"address": a, "kinds": _ordered(kinds[a])} for a in failed],
            "no_metadata": [
                {"address": a, "kinds": _ordered(kinds[a])} for a in addresses if a not in results and a not in failed_set
            ],
        },
    )
    print(
        f"{len(results):,} of {len(addresses):,} address(es) have metadata; added {added:,} funder address(es) "
        f"to addresses; wrote {OUTPUT_PATH.relative_to(GATHER_ROOT)}"
    )
    if failed:
        print(f"{len(failed):,} address(es) couldn't be looked up -- listed in {OUTPUT_PATH.name}", file=sys.stderr)


if __name__ == "__main__":
    main()
