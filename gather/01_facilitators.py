#!/usr/bin/env python3
"""Gathers facilitators: scrapes x402scan.com's Facilitators page (and each
facilitator's own page, for its addresses), adds API and doc URLs from the
table below, fingerprints every URL, and stores the result in gather's
database and in dataset/facilitators.json.

Steps:
  1. Check nothing's been gathered yet: exits, changing nothing, if
     dataset/facilitators.json exists or the facilitator table has rows.
  2. Scrape x402scan.com/facilitators -- the same scrape as step 2 of
     x402-analysis-api/scripts/update.py (gatherlib/x402scan_scraper.py).
     x402scan gives each facilitator's name, docs URL, chains and
     addresses, but no API URL.
  3. Add API and doc URLs from KNOWN_FACILITATORS below (copied from
     x402-analysis-api/scripts/data/x402Fac.json), matched to x402scan's
     facilitators by x402scan id, or failing that by name. Where both
     have a doc URL, the table's wins. A facilitator in the table that
     x402scan doesn't list is still gathered, with no addresses.
  4. Fingerprint every URL (api, doc, x402scan page): IP address,
     geolocation and TLS certificate subject.
  5. Write facilitators, their URLs (uris) and their addresses (addresses,
     linkaddresses) to the database in one transaction, then write
     dataset/facilitators.json.

Ethereum-style addresses (0x + 40 hex digits) are stored in lower case;
other addresses (e.g. Solana's, which are case-sensitive) as they are.
Facilitators are stored as not active -- 03_facilitator_liveness_check.py
checks them.

Usage:
    python gather/01_facilitators.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

GATHER_ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    import psycopg2  # noqa: F401
    import requests  # noqa: F401
except ImportError as exc:
    print(
        f"missing required package: {exc.name}. Run `pip install -r gather/requirements.txt`, "
        "with gather's virtualenv active.",
        file=sys.stderr,
    )
    sys.exit(1)

load_dotenv(GATHER_ROOT / ".env")

from gatherlib import DATASET_DIR, localdb, preflight, x402scan_scraper  # noqa: E402
from gatherlib.common import (  # noqa: E402
    OWNER_FACILITATOR,
    dot,
    exit_if_exists,
    fingerprint_urls,
    get_or_create_address,
    link_address,
    normalize_address,
    sanitize,
    table_has_rows,
    upsert_uri,
    write_json,
)

OUTPUT_PATH = DATASET_DIR / "facilitators.json"
SOURCE_X402SCAN = "x402scan"
SOURCE_TABLE = "built-in list"

# (name, x402scan id, api url, doc url), from
# x402-analysis-api/scripts/data/x402Fac.json.
KNOWN_FACILITATORS: list[tuple[str, str | None, str | None, str | None]] = [
    ("402104", None, "https://x402.load.network", "https://x402.load.network"),
    ("Canton", None, "https://facilitator.ftptech.xyz", "https://www.ftptech.xyz/x402"),
    ("Celo Facilitator", None, "https://api.x402.celo.org", "https://x402.celo.org"),
    ("CodeNut", None, "https://facilitator.codenut.ai", "https://docs.codenut.ai/guides/x402-facilitator"),
    (
        "Coinbase",
        "coinbase",
        "https://api.cdp.coinbase.com/platform",
        "https://docs.cdp.coinbase.com/api-reference/v2/rest-api/x402-facilitator/x402-facilitator",
    ),
    ("Daydreams", None, "https://facilitator.daydreams.systems", "https://facilitator.daydreams.systems"),
    ("Dexter", "dexter", "https://facilitator.dexter.cash", "https://facilitator.dexter.cash"),
    ("FareSide", None, "https://facilitator.x402.rs", "https://x402.rs"),
    ("Figment", "figment-facilitator", "https://api.figment.io/x402", "https://docs.figment.io/reference/x402"),
    ("FluxA", "fluxa", "https://facilitator.fluxapay.xyz", "https://facilitator.fluxapay.xyz"),
    ("Heurist", None, "https://facilitator.heurist.xyz", "https://docs.heurist.ai/x402-products/facilitator"),
    ("HPP", None, "https://facilitator.hpp.io", "https://docs.hpp.io/x402/facilitator"),
    ("KAMIYO", None, "https://kamiyo.ai/api/v1/x402", "https://kamiyo.ai/docs"),
    ("Meridian Facilitator", "mrdn", "https://api.mrdn.finance/v1", "https://mrdn.finance"),
    ("Mogami", None, "https://facilitator.mogami.tech", "https://mogami.tech/"),
    ("PayAI", "payAI", "https://facilitator.payai.network", "https://payai.network"),
    (
        "Polygon Facilitator",
        None,
        "https://x402.polygon.technology",
        "https://agentic-docs.polygon.technology/general/x402/intro/",
    ),
    ("Polymer", "polymer", "https://api.polymer.zone/v1", "https://docs.polymerlabs.org/docs/build/start"),
    ("Primer", "primer", "https://x402.primer.systems", "https://docs.primer.systems/facilitator.html"),
    ("Questflow", None, "https://facilitator.questflow.ai", "https://facilitator.questflow.ai"),
    ("Solvador", None, "https://api.solvador.com", "https://solvador.com"),
    ("Stellar", None, "https://channels.openzeppelin.com/x402", "https://channels.openzeppelin.com/x402"),
    ("T54", None, "https://xrpl-facilitator-mainnet.t54.ai", "https://docs.x402.org/dev-tools/facilitators"),
    (
        "Thirdweb",
        None,
        "https://api.thirdweb.com/v1/payments/x402",
        "https://portal.thirdweb.com/payments/x402/facilitator",
    ),
    ("three.ws", "three-ws", None, "https://three.ws/docs/x402-distribution"),
    (
        "Ultravioleta DAO",
        "ultravioletadao",
        "https://facilitator.ultravioletadao.xyz",
        "https://facilitator.ultravioletadao.xyz",
    ),
    ("Unknown (Near)", None, "https://x402.mikedotexe.com", "https://x402.mikedotexe.com"),
    ("Virtuals Protocol", "virtuals", None, "https://app.virtuals.io"),
]


def check_not_gathered() -> None:
    exit_if_exists(OUTPUT_PATH)
    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur:
            if table_has_rows(cur, "facilitator"):
                print("the facilitator table already has rows -- nothing done.", file=sys.stderr)
                sys.exit(2)
    finally:
        conn.close()


def merge(scraped: list[dict]) -> list[dict]:
    """x402scan's facilitators with the table's API/doc URLs added, plus
    any facilitator in the table that x402scan doesn't list, sorted by
    name."""
    by_id = {fid.lower(): entry for entry in KNOWN_FACILITATORS if (fid := entry[1])}
    by_name = {entry[0].lower(): entry for entry in KNOWN_FACILITATORS}
    used: set[str] = set()
    facilitators: list[dict] = []

    for item in scraped:
        name = item.get("name")
        if not name:
            continue
        known = by_id.get((item.get("id") or "").lower()) or by_name.get(name.lower())
        if known:
            used.add(known[0])
        addresses = sorted({normalize_address(a) for a in item.get("addresses") or [] if a and a.strip()})
        facilitators.append(
            {
                "name": name,
                "x402scan_id": item.get("id"),
                "api_url": known[2] if known else None,
                "doc_url": (known[3] if known else None) or item.get("docs_url"),
                "x402scan_url": item.get("url"),
                "chains": item.get("chains") or [],
                "addresses": addresses,
                "sources": [SOURCE_X402SCAN] + ([SOURCE_TABLE] if known else []),
                "errors": item.get("errors") or [],
            }
        )

    for name, x402scan_id, api_url, doc_url in KNOWN_FACILITATORS:
        if name in used:
            continue
        facilitators.append(
            {
                "name": name,
                "x402scan_id": x402scan_id,
                "api_url": api_url,
                "doc_url": doc_url,
                "x402scan_url": None,
                "chains": [],
                "addresses": [],
                "sources": [SOURCE_TABLE],
                "errors": [],
            }
        )

    facilitators.sort(key=lambda f: f["name"].lower())
    return facilitators


def store(facilitators: list[dict], fingerprints: dict[str, dict]) -> None:
    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur:
            # Checked again inside the transaction, in case another run
            # started meanwhile.
            if table_has_rows(cur, "facilitator"):
                print("the facilitator table already has rows -- nothing done.", file=sys.stderr)
                sys.exit(2)
            print(f"storing {len(facilitators)} facilitator(s) ", end="", flush=True)
            for f in facilitators:
                cur.execute(
                    """
                    INSERT INTO facilitator (name, api, doc, x402scan, active)
                    VALUES (%s, %s, %s, %s, FALSE)
                    RETURNING id
                    """,
                    (
                        f["name"],
                        upsert_uri(cur, f["api_url"], fingerprints),
                        upsert_uri(cur, f["doc_url"], fingerprints),
                        upsert_uri(cur, f["x402scan_url"], fingerprints),
                    ),
                )
                facilitator_id = cur.fetchone()[0]
                for address in f["addresses"]:
                    link_address(cur, OWNER_FACILITATOR, facilitator_id, get_or_create_address(cur, address, SOURCE_X402SCAN))
                dot()
            print()
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    preflight.check_or_exit(localdb.preflight_checks)
    check_not_gathered()

    print("scraping x402scan.com/facilitators ", end="", flush=True)
    scraped = sanitize(x402scan_scraper.scrape_all_facilitators(on_progress=dot))
    print()
    print(f"scraped {len(scraped)} facilitator(s) from x402scan")
    for item in scraped:
        for error in item.get("errors") or []:
            print(f"  note: {item.get('name')}: {error}")

    facilitators = merge(scraped)
    matched = sum(1 for f in facilitators if f["sources"] == [SOURCE_X402SCAN, SOURCE_TABLE])
    x402scan_only = sum(1 for f in facilitators if f["sources"] == [SOURCE_X402SCAN])
    table_only = sum(1 for f in facilitators if f["sources"] == [SOURCE_TABLE])
    print(
        f"{len(facilitators)} facilitator(s): {matched} on x402scan and in the built-in list, "
        f"{x402scan_only} only on x402scan, {table_only} only in the built-in list"
    )

    urls = {url for f in facilitators for url in (f["api_url"], f["doc_url"], f["x402scan_url"]) if url}
    fingerprints = fingerprint_urls(urls)

    store(facilitators, fingerprints)

    for f in facilitators:
        f["fingerprints"] = {
            url: fingerprints[url] for url in (f["api_url"], f["doc_url"], f["x402scan_url"]) if url
        }
    write_json(
        OUTPUT_PATH,
        {
            "gathered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "facilitators": facilitators,
        },
    )

    addresses = {a for f in facilitators for a in f["addresses"]}
    with_api = sum(1 for f in facilitators if f["api_url"])
    print(
        f"stored {len(facilitators)} facilitator(s) ({with_api} with an API URL) and "
        f"{len(addresses)} unique address(es); wrote {OUTPUT_PATH.relative_to(GATHER_ROOT)}"
    )


if __name__ == "__main__":
    main()
