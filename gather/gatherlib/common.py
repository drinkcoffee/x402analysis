"""Helpers shared by gather's numbered scripts: cleaning scraped data,
normalizing addresses, fingerprinting URLs, writing rows, and writing the
dataset JSON files.

fingerprint_urls and the row-writing helpers are adapted from
x402-analysis-api/scripts/load_facilitators.py and update.py (copied, so
gather doesn't depend on them).
"""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import net_analysis

OWNER_FACILITATOR = 0
OWNER_SERVER = 1

NET_TIMEOUT = 8.0
NET_MAX_WORKERS = 10

ETH_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def sanitize(value):
    """Strips NUL (0x00) characters from strings, recursively through
    lists/dicts. Postgres text columns can't store them, and scraped
    descriptions occasionally contain them."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    return value


def normalize_address(address: str) -> str:
    """Ethereum-style addresses (0x + 40 hex digits) in lower case; any
    other address (e.g. Solana's base58, which is case-sensitive) as is."""
    address = address.strip()
    return address.lower() if ETH_ADDRESS.match(address) else address


def dot() -> None:
    print(".", end="", flush=True)


def fingerprint_urls(urls: set[str]) -> dict[str, dict]:
    """url -> {"ip", "location", "subject"} for every url: DNS resolution,
    a TLS handshake for the certificate subject (commonName, falling back
    to organizationName), and a batched ip-api.com geolocation lookup. Each
    hostname is probed once, however many urls share it. Prints a "." per
    hostname."""
    hostname_by_url = {url: net_analysis.parse_hostname(url) for url in urls}
    unique_hosts = sorted({host for host in hostname_by_url.values() if host})

    print(f"fingerprinting {len(unique_hosts)} unique hostname(s) ", end="", flush=True)
    host_info: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=NET_MAX_WORKERS) as pool:
        futures = {pool.submit(net_analysis.analyse_host, host, NET_TIMEOUT): host for host in unique_hosts}
        for future in as_completed(futures):
            host = futures[future]
            dot()
            try:
                host_info[host] = future.result()
            except Exception as exc:  # noqa: BLE001
                host_info[host] = {"ip": None, "ssl": {"error": str(exc)}}
    print()

    ips = [info["ip"] for info in host_info.values() if info.get("ip")]
    if ips:
        print(f"geolocating {len(set(ips))} IP address(es) ...", flush=True)
    geo_by_ip = net_analysis.geolocate_batch(ips, timeout=NET_TIMEOUT * 2) if ips else {}

    fingerprints: dict[str, dict] = {}
    for url, host in hostname_by_url.items():
        info = host_info.get(host, {})
        ip = info.get("ip")
        ssl_info = info.get("ssl") or {}
        fingerprints[url] = {
            "ip": ip,
            "location": _location_string(geo_by_ip.get(ip)) if ip else None,
            "subject": ssl_info.get("subject_cn") or ssl_info.get("subject_o"),
        }
    return fingerprints


def _location_string(geo: dict | None) -> str | None:
    if not geo or geo.get("status") != "success":
        return None
    parts = [geo.get("city"), geo.get("regionName"), geo.get("country")]
    return ", ".join(p for p in parts if p) or None


# --- Writing rows ---------------------------------------------------------


def table_has_rows(cur, table: str) -> bool:
    cur.execute(f"SELECT EXISTS (SELECT 1 FROM {table})")
    return cur.fetchone()[0]


def upsert_uri(cur, url: str | None, fingerprints: dict[str, dict]) -> int | None:
    """The id of `url`'s uris row, inserting it (with its fingerprint, if
    it has one) if it's new. None for no url."""
    if not url:
        return None
    fp = fingerprints.get(url) or {}
    cur.execute(
        """
        INSERT INTO uris (url, ip, location, subject)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (url) DO UPDATE SET url = EXCLUDED.url
        RETURNING id
        """,
        (url, fp.get("ip"), fp.get("location"), fp.get("subject")),
    )
    return cur.fetchone()[0]


def get_or_create_address(cur, address: str, source: str) -> int:
    """The id of `address`'s addresses row (address already normalized),
    inserting it if it's new; `source` is added to an existing row's
    sources if it isn't listed yet."""
    cur.execute(
        """
        INSERT INTO addresses (address, source)
        VALUES (%s, %s)
        ON CONFLICT (address) DO UPDATE
            SET source = CASE
                WHEN addresses.source IS NULL THEN EXCLUDED.source
                WHEN EXCLUDED.source = ANY (string_to_array(addresses.source, ', ')) THEN addresses.source
                ELSE addresses.source || ', ' || EXCLUDED.source
            END
        RETURNING id
        """,
        (address, source),
    )
    return cur.fetchone()[0]


def link_address(cur, owner: int, ref: int, address_id: int) -> None:
    cur.execute(
        """
        INSERT INTO linkaddresses (owner, ref, address)
        VALUES (%s, %s, %s)
        ON CONFLICT (owner, ref, address) DO NOTHING
        """,
        (owner, ref, address_id),
    )


# --- Dataset files --------------------------------------------------------


def exit_if_exists(path: Path) -> None:
    if path.exists():
        print(f"{path} already exists -- delete it to gather again.", file=sys.stderr)
        sys.exit(2)


def write_json(path: Path, data) -> None:
    """Writes `data` as indented JSON, via a temporary file renamed into
    place, so `path` is never left half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
