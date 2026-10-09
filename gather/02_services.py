#!/usr/bin/env python3
"""Gathers servers and the services (resources) they offer, from two
sources merged by hostname, and stores them in gather's database and in
dataset/services.json. The same discovery as step 5 of
x402-analysis-api/scripts/update.py.

Steps:
  1. Check nothing's been gathered yet: exits, changing nothing, if
     dataset/services.json exists or the server table has rows.
  2. Scrape x402scan.com's server listing and every server's own page
     (gatherlib/x402scan_scraper.py's scrape_all).
  3. Fetch every resource in Coinbase's CDP Bazaar
     (GET /v2/x402/discovery/resources, unauthenticated, paged).
  4. Merge the two by the hostname of each server's API URL. x402scan's
     data wins for a path both report (it has tags and a cleaner price);
     a server with no name from x402scan is named after its hostname. Each
     service gets a category from gatherlib/service_classifier.py's
     keyword rules.
  5. Fingerprint every server's API and doc URL: IP address, geolocation
     and TLS certificate subject.
  6. Write servers, services, their URLs (uris) and the addresses they're
     paid to (addresses, linkaddresses) to the database in one
     transaction, then write dataset/services.json.

Ethereum-style addresses (0x + 40 hex digits) are stored in lower case;
other addresses (e.g. Solana's, which are case-sensitive) as they are.
Each address's source is "x402scan", "Bazaar", or both. Servers and
services are stored as not active, since nothing here probes them.

Server names must be unique, but two hostnames can share an x402scan
title; the second and later get " (<hostname>)" added to their name.

Usage:
    python gather/02_services.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

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

from gatherlib import DATASET_DIR, localdb, preflight, service_classifier, x402scan_scraper  # noqa: E402
from gatherlib.cdp_client import CdpClient  # noqa: E402
from gatherlib.common import (  # noqa: E402
    OWNER_SERVER,
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

OUTPUT_PATH = DATASET_DIR / "services.json"
SOURCE_X402SCAN = "x402scan"
SOURCE_BAZAAR = "Bazaar"
BAZAAR_PAGE_LIMIT = 200


def check_not_gathered() -> None:
    exit_if_exists(OUTPUT_PATH)
    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur:
            if table_has_rows(cur, "server"):
                print("the server table already has rows -- nothing done.", file=sys.stderr)
                sys.exit(2)
    finally:
        conn.close()


def _hostname(url: str | None) -> str | None:
    return (urlparse(url or "").hostname or "").lower() or None


def fetch_bazaar_resources() -> list[dict]:
    """Every resource in Coinbase's CDP Bazaar, page by page (the offset
    advances by however many each page returned, stopping at an empty page
    or pagination.total). Prints a "." per page."""
    client = CdpClient()
    items: list[dict] = []
    offset = 0
    while True:
        response = client.discovery_resources(limit=BAZAAR_PAGE_LIMIT, offset=offset)
        response.raise_for_status()
        body = response.json()
        batch = body.get("items") or []
        items.extend(batch)
        offset += len(batch)
        dot()
        total = (body.get("pagination") or {}).get("total")
        if not batch or (total is not None and offset >= total):
            break
    return items


def _format_bazaar_price(accepts: list[dict] | None) -> str | None:
    """Bazaar's payment options as one string, e.g. "1000 USDC on base;
    500 USDC on base-sepolia"."""
    parts = []
    for accept in accepts or []:
        amount, asset, network = accept.get("amount"), accept.get("asset"), accept.get("network")
        if amount and asset:
            parts.append(f"{amount} {asset}" + (f" on {network}" if network else ""))
    return "; ".join(parts) or None


def _classify(full_url: str | None, description: str | None, tags: list[str]) -> str:
    text = service_classifier.resource_text({"url": full_url, "description": description, "tags": tags})
    return service_classifier.classify_resource(text)


def build_servers(x402scan_servers: list[dict], bazaar_items: list[dict]) -> list[dict]:
    """One entry per hostname, merging both sources, sorted by name:
    {"name", "hostname", "api_url", "doc_url", "addresses": {address:
    [sources]}, "services": [{"path", "url", "description", "price",
    "tags", "method", "category", "source"}]}."""
    servers: dict[str, dict] = {}

    def _server(hostname: str) -> dict:
        return servers.setdefault(
            hostname,
            {"name": None, "hostname": hostname, "api_url": None, "doc_url": None, "addresses": {}, "services": {}},
        )

    def _add_address(entry: dict, address: str | None, source: str) -> None:
        if address and address.strip():
            sources = entry["addresses"].setdefault(normalize_address(address), [])
            if source not in sources:
                sources.append(source)

    for group in x402scan_servers:
        hostname = _hostname(group.get("api_url"))
        if not hostname:
            continue
        entry = _server(hostname)
        entry["name"] = group.get("name") or entry["name"]
        entry["api_url"] = entry["api_url"] or group.get("api_url")
        entry["doc_url"] = entry["doc_url"] or group.get("doc_url")
        for address in group.get("addresses") or []:
            _add_address(entry, address, SOURCE_X402SCAN)
        for resource in group.get("resources") or []:
            url = resource.get("url")
            path = urlparse(url or "").path
            if not path:
                continue
            entry["services"][path] = {
                "path": path,
                "url": url,
                "description": resource.get("description"),
                "price": resource.get("price"),
                "tags": resource.get("tags") or [],
                "method": resource.get("method"),
                "source": SOURCE_X402SCAN,
            }

    for item in bazaar_items:
        url = item.get("resource")
        hostname = _hostname(url)
        if not hostname:
            continue
        parsed = urlparse(url)
        entry = _server(hostname)
        entry["api_url"] = entry["api_url"] or f"{parsed.scheme}://{parsed.netloc}"
        for accept in item.get("accepts") or []:
            _add_address(entry, accept.get("payTo"), SOURCE_BAZAAR)
        if not parsed.path or parsed.path in entry["services"]:
            continue
        entry["services"][parsed.path] = {
            "path": parsed.path,
            "url": url,
            "description": item.get("description"),
            "price": _format_bazaar_price(item.get("accepts")),
            "tags": [],
            "method": None,
            "source": SOURCE_BAZAAR,
        }

    result = []
    for hostname, entry in servers.items():
        entry["name"] = entry["name"] or hostname
        services = sorted(entry["services"].values(), key=lambda s: s["path"])
        for service in services:
            service["category"] = _classify(service["url"], service["description"], service["tags"])
        entry["services"] = services
        entry["addresses"] = dict(sorted(entry["addresses"].items()))
        result.append(entry)

    # Unique names: the first server (by hostname) keeps a shared name.
    result.sort(key=lambda s: (s["name"].lower(), s["hostname"]))
    used: set[str] = set()
    for entry in result:
        if entry["name"].lower() in used:
            entry["name"] = f"{entry['name']} ({entry['hostname']})"
        used.add(entry["name"].lower())
    return result


def store(servers: list[dict], fingerprints: dict[str, dict]) -> None:
    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur:
            # Checked again inside the transaction, in case another run
            # started meanwhile.
            if table_has_rows(cur, "server"):
                print("the server table already has rows -- nothing done.", file=sys.stderr)
                sys.exit(2)
            print(f"storing {len(servers)} server(s) ", end="", flush=True)
            for n, s in enumerate(servers, 1):
                cur.execute(
                    """
                    INSERT INTO server (name, api, doc, active)
                    VALUES (%s, %s, %s, FALSE)
                    RETURNING id
                    """,
                    (s["name"], upsert_uri(cur, s["api_url"], fingerprints), upsert_uri(cur, s["doc_url"], fingerprints)),
                )
                server_id = cur.fetchone()[0]
                for service in s["services"]:
                    cur.execute(
                        """
                        INSERT INTO service (server, path, description, price, tags, category, active)
                        VALUES (%s, %s, %s, %s, %s, %s, FALSE)
                        """,
                        (
                            server_id,
                            service["path"],
                            service["description"],
                            service["price"],
                            ", ".join(service["tags"]) or None,
                            service["category"],
                        ),
                    )
                for address, sources in s["addresses"].items():
                    for source in sources:
                        address_id = get_or_create_address(cur, address, source)
                    link_address(cur, OWNER_SERVER, server_id, address_id)
                if n % 25 == 0:
                    dot()
            print()
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    preflight.check_or_exit(localdb.preflight_checks)
    check_not_gathered()

    print("scraping x402scan.com's servers (a \".\" per server) ", end="", flush=True)
    x402scan_servers = sanitize(x402scan_scraper.scrape_all(on_progress=dot))
    print()
    print(f"scraped {len(x402scan_servers)} server(s) from x402scan")

    print("fetching Coinbase CDP Bazaar's resources (a \".\" per page) ", end="", flush=True)
    bazaar_items = sanitize(fetch_bazaar_resources())
    print()
    print(f"fetched {len(bazaar_items)} resource(s) from Bazaar")

    servers = build_servers(x402scan_servers, bazaar_items)
    service_count = sum(len(s["services"]) for s in servers)
    print(f"merged into {len(servers)} server(s) offering {service_count} service(s)")

    urls = {url for s in servers for url in (s["api_url"], s["doc_url"]) if url}
    fingerprints = fingerprint_urls(urls)

    store(servers, fingerprints)

    for s in servers:
        s["fingerprints"] = {url: fingerprints[url] for url in (s["api_url"], s["doc_url"]) if url}
    write_json(
        OUTPUT_PATH,
        {
            "gathered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "servers": servers,
        },
    )

    addresses = {a for s in servers for a in s["addresses"]}
    print(
        f"stored {len(servers)} server(s), {service_count} service(s) and {len(addresses)} unique "
        f"address(es); wrote {OUTPUT_PATH.relative_to(GATHER_ROOT)}"
    )


if __name__ == "__main__":
    main()
