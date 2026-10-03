#!/usr/bin/env python3
"""Loads scripts/data/x402Fac.json into the database (facilitator, uris,
addresses, linkaddresses tables).

Field mapping, per facilitator entry in the JSON:
    name        -> facilitator.name (the upsert key -- see below)
    api_url     -> a uris row, referenced by facilitator.api
    doc_url     -> a uris row, referenced by facilitator.doc
    x402scan_url -> a uris row, referenced by facilitator.x402scan (a "new
                   type of URL" alongside api/doc/website)
    addresses[].address -> an addresses row, linked via linkaddresses
                           (owner=0, i.e. "facilitator")
    addresses[].chains  -> addresses.chains, comma-separated if more than one
    addresses[].sources -> addresses.source, comma-separated if more than one

Before touching any data, this also brings the database's schema up to
date (db/schema.sql then db/migrations.sql -- see scripts/db_setup.py),
the same thing scripts/init_db.py does. Running that separately first is
no longer required, precisely because forgetting to is what caused
`psycopg2.errors.InvalidColumnReference: there is no unique or exclusion
constraint matching the ON CONFLICT specification` the first time this
script shipped: schema.sql had gained a UNIQUE constraint this script
needs, but an already-created database didn't have it yet, and this
script alone couldn't fix that until now.

Every URL that becomes a uris row is also passively fingerprinted -- IP
address, IP geolocation, and TLS certificate subject name -- reusing the
sibling cli-tool project's x402tool.net_analysis module (DNS resolution, a
TLS handshake, and a batched ip-api.com lookup; see that module's docstring
for details). This is a local-only maintenance script, never deployed to
Vercel, so importing across the two sibling projects (rather than
duplicating that logic here) is fine -- it assumes cli-tool/ is checked out
next to this project, as it is in this monorepo.

Not stored anywhere (see README.md for why): each facilitator's own
top-level "chains" list, and its "aliases" list -- neither has a matching
column on the facilitator table, and the decision was to drop them rather
than grow the schema further or stuff free text into `notes`.

Idempotent: safe to re-run after x402Fac.json is refreshed (or just to
refresh IP/geo/TLS fingerprints, since those can change over time).
  - uris (unique on url) and addresses (unique on address) are upserted,
    so the same URL/address is never duplicated across runs or across
    facilitators that happen to share one. uris.ip/location/subject ARE
    overwritten on conflict (they're re-fingerprinted every run), but
    uris.risk/notes are left alone -- manual curation, not this loader's.
  - facilitator is upserted by name (unique). Only the columns this loader
    owns (api/doc/website/x402scan, updated) are overwritten on conflict --
    risk/active/notes are left untouched, since those are for manual
    curation (e.g. via a future admin UI) and shouldn't be silently reset
    by a data refresh.
  - addresses.chains/source ARE overwritten on conflict (they come from
    this same raw source data), but addresses.risk/notes are left alone
    for the same manual-curation reason as above.
  - linkaddresses has a UNIQUE(owner, ref, address) constraint, so
    re-linking an address already linked to a facilitator is a no-op
    rather than a duplicate row.

Usage:
    python scripts/load_facilitators.py
    python scripts/load_facilitators.py path/to/other.json

Requires DATABASE_URL, read from a .env file in the project root (if
present) or the real environment.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI_TOOL_ROOT = PROJECT_ROOT.parent / "cli-tool"

import preflight  # noqa: E402

try:
    from dotenv import load_dotenv
    import psycopg2
except ImportError as exc:
    print(
        f"missing required package: {exc.name}. Run `pip install -r "
        "requirements.txt` from x402-analysis-api/, with your virtualenv "
        "active.",
        file=sys.stderr,
    )
    sys.exit(1)

load_dotenv(PROJECT_ROOT / ".env")

import os  # noqa: E402

from db_setup import ensure_schema  # noqa: E402

sys.path.insert(0, str(CLI_TOOL_ROOT))
try:
    from x402tool import net_analysis  # noqa: E402
except ImportError as exc:
    print(
        f"couldn't import the sibling cli-tool project's x402tool.net_analysis "
        f"module ({exc}). Clone cli-tool next to this project (as it is in "
        f"this monorepo) -- expected at {CLI_TOOL_ROOT}.",
        file=sys.stderr,
    )
    sys.exit(1)

DEFAULT_DATA_PATH = PROJECT_ROOT / "scripts" / "data" / "x402Fac.json"
NET_TIMEOUT = 8.0
NET_MAX_WORKERS = 10

OWNER_FACILITATOR = 0


def _location_string(geo: dict | None) -> str | None:
    if not geo or geo.get("status") != "success":
        return None
    parts = [geo.get("city"), geo.get("regionName"), geo.get("country")]
    return ", ".join(p for p in parts if p) or None


def fingerprint_urls(urls: set[str]) -> dict[str, dict]:
    """url -> {"ip", "location", "subject"} for every url in `urls`, via
    net_analysis: DNS resolution, a TLS handshake for the certificate
    subject (commonName, falling back to organizationName), and a batched
    IP geolocation lookup. One hostname is only ever probed once even if
    several urls share it (e.g. an api_url and doc_url on the same host)."""
    hostname_by_url = {url: net_analysis.parse_hostname(url) for url in urls}
    unique_hosts = sorted({host for host in hostname_by_url.values() if host})

    print(f"fingerprinting {len(unique_hosts)} unique hostname(s)...", end="", flush=True)
    host_info: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=NET_MAX_WORKERS) as pool:
        futures = {pool.submit(net_analysis.analyse_host, host, NET_TIMEOUT): host for host in unique_hosts}
        for future in as_completed(futures):
            host = futures[future]
            print(".", end="", flush=True)
            try:
                host_info[host] = future.result()
            except Exception as exc:  # noqa: BLE001
                host_info[host] = {"ip": None, "ssl": {"error": str(exc)}}
    print()

    ips = [info["ip"] for info in host_info.values() if info.get("ip")]
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


def _upsert_uri(cur, url: str, fingerprints: dict[str, dict]) -> int:
    """Inserts `url` into uris if it's new, and returns its id either way.

    An existing row's ip/location/subject are only overwritten if `url` was
    actually fingerprinted this run (is a key in `fingerprints`). Some
    callers -- update.py's _get_or_create_server/_get_or_create_facilitator
    -- pass along a URL that was never fingerprinted, and that mustn't
    wipe the fingerprint already stored for it."""
    if url not in fingerprints:
        cur.execute(
            """
            INSERT INTO uris (url) VALUES (%s)
            ON CONFLICT (url) DO UPDATE SET url = EXCLUDED.url
            RETURNING id
            """,
            (url,),
        )
        return cur.fetchone()[0]

    fp = fingerprints[url]
    cur.execute(
        """
        INSERT INTO uris (url, ip, location, subject)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (url) DO UPDATE
            SET ip = EXCLUDED.ip,
                location = EXCLUDED.location,
                subject = EXCLUDED.subject,
                updated = CURRENT_DATE
        RETURNING id
        """,
        (url, fp.get("ip"), fp.get("location"), fp.get("subject")),
    )
    return cur.fetchone()[0]


def _uri_id_or_none(cur, url: str | None, fingerprints: dict[str, dict]) -> int | None:
    return _upsert_uri(cur, url, fingerprints) if url else None


def _upsert_facilitator(cur, name: str, api: int | None, doc: int | None, x402scan: int | None) -> int:
    cur.execute(
        """
        INSERT INTO facilitator (name, api, doc, x402scan)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (name) DO UPDATE
            SET api = EXCLUDED.api,
                doc = EXCLUDED.doc,
                x402scan = EXCLUDED.x402scan,
                updated = CURRENT_DATE
        RETURNING id
        """,
        (name, api, doc, x402scan),
    )
    return cur.fetchone()[0]


def _upsert_address(cur, address: str, chains: str | None, source: str | None) -> int:
    cur.execute(
        """
        INSERT INTO addresses (address, chains, source)
        VALUES (%s, %s, %s)
        ON CONFLICT (address) DO UPDATE
            SET chains = EXCLUDED.chains,
                source = EXCLUDED.source,
                updated = CURRENT_DATE
        RETURNING id
        """,
        (address, chains, source),
    )
    return cur.fetchone()[0]


def _link_address(cur, owner: int, ref: int, address_id: int) -> None:
    cur.execute(
        """
        INSERT INTO linkaddresses (owner, ref, address)
        VALUES (%s, %s, %s)
        ON CONFLICT (owner, ref, address) DO NOTHING
        """,
        (owner, ref, address_id),
    )


def load_facilitators(cur, facilitators: list[dict]) -> tuple[int, int]:
    """Returns (facilitator_count, address_link_count)."""
    urls = {
        url
        for entry in facilitators
        for url in (entry.get("api_url"), entry.get("doc_url"), entry.get("x402scan_url"))
        if url
    }
    fingerprints = fingerprint_urls(urls)

    address_link_count = 0

    for entry in facilitators:
        api_id = _uri_id_or_none(cur, entry.get("api_url"), fingerprints)
        doc_id = _uri_id_or_none(cur, entry.get("doc_url"), fingerprints)
        x402scan_id = _uri_id_or_none(cur, entry.get("x402scan_url"), fingerprints)

        facilitator_id = _upsert_facilitator(cur, entry["name"], api_id, doc_id, x402scan_id)

        for addr in entry.get("addresses") or []:
            chains = ", ".join(addr.get("chains") or []) or None
            source = ", ".join(addr.get("sources") or []) or None
            address_id = _upsert_address(cur, addr["address"], chains, source)
            _link_address(cur, OWNER_FACILITATOR, facilitator_id, address_id)
            address_link_count += 1

    return len(facilitators), address_link_count


def main() -> None:
    data_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DATA_PATH
    database_url = os.getenv("DATABASE_URL")
    preflight.check_or_exit(
        lambda: None if data_path.exists() else f"{data_path} does not exist.",
        preflight.require_database_url(database_url),
    )

    facilitators = json.loads(data_path.read_text())

    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            ensure_schema(cur)
            facilitator_count, address_link_count = load_facilitators(cur, facilitators)
        conn.commit()
    finally:
        conn.close()

    print(f"loaded {facilitator_count} facilitators and {address_link_count} facilitator-address links from {data_path}")


if __name__ == "__main__":
    main()
