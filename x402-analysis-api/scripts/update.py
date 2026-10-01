#!/usr/bin/env python3
"""Refreshes facilitator data from x402scan.com and re-checks which
facilitators are actually live, writing any changes to both the local
Postgres mirror and Neon.

Steps:
  1. Make sure the local database exists (download_db.download(), if it
     hasn't been downloaded yet) and its server is running.
  2. Scrape x402scan.com's Facilitators page (x402scan_scraper.py, vendored
     from the sibling cli-tool project -- see that file's docstring).
  3. Compare the scrape against the local database:
       - a facilitator x402scan knows about that the database doesn't ->
         a new facilitator row (api URL left unset -- x402scan's data
         doesn't include one, only a doc URL and its own x402scan.com page
         URL; see x402scan_scraper.py's docstring for why).
       - a doc/x402scan URL that's different from what's stored -> updated.
       - an address x402scan lists that isn't already linked to that
         facilitator -> added (never removed -- x402scan not re-listing an
         address isn't treated as evidence it's no longer valid).
  4. Re-check every facilitator's liveness the same way check_supported.py
     does (imported from it directly, not shelled out to, so this reuses
     its exact CDP-authentication and JSON-validity logic without
     re-parsing printed output): a facilitator with no api URL is not
     active; one with an api URL whose /supported response isn't valid
     JSON is not active; anything else is active. Wherever that disagrees
     with the stored `active` value, it's updated.
  5. Scan for servers and the services (resources) they offer, from two
     sources merged by hostname (build_discovered_servers): x402scan.com's
     homepage listing (x402scan_scraper.scrape_all() -- already vendored,
     just not previously used for anything but facilitators) and Coinbase's
     CDP Bazaar (GET /v2/x402/discovery/resources, unauthenticated --
     fetch_bazaar_resources()). A server neither database knows about
     becomes a new row (api URL is whichever source has one; a server with
     no name from x402scan just gets its hostname as a placeholder name); a
     service (path) not already recorded under its server is added, with a
     category from scripts/service_classifier.py's keyword rules
     (vendored from cli-tool, same as cdp_client.py/x402scan_scraper.py)
     and, since actually confirming a paid resource works would require a
     real payment, an `active` flag based on whether an unpaid request to
     it got back HTTP 402 (the correct response from a live x402-gated
     endpoint) rather than a connection failure or something else. Existing
     services aren't touched, and an existing server is only ever upgraded
     to active by a newly found live service, never downgraded (see
     build_server_ops's docstring for why).
  6. Every change from steps 3-5 is applied to *both* the local database
     and Neon -- computed once (against the local mirror, which is assumed
     to already match Neon, since this script is the only thing that's
     supposed to write to either), then replayed identically against each
     connection so the two never drift apart. Each connection resolves its
     own foreign keys via upsert-by-natural-key (url/address/name), so the
     two databases' own internal row ids never need to match.

This isn't a distributed transaction: the local commit and the Neon commit
are two separate commits, in that order. If the Neon commit fails after the
local one succeeds, the two databases are left temporarily out of sync
until the next run (which would just re-derive and re-apply the same
still-pending changes to Neon) -- acceptable for a periodically-run
maintenance script, not something to build real two-phase-commit machinery
around.

Step 3's diff is only ever safe to apply to Neon if the local mirror it was
computed against actually reflects *that* Neon database. If DATABASE_URL
ever points somewhere else than whatever the local mirror was last
downloaded from -- a different Neon project/branch, or a stale local mirror
left over from testing against a throwaway database -- every facilitator
that Neon already has but the (wrong) local mirror doesn't looks "new,"
and applying that diff to Neon overwrites those facilitators' real api/doc/
x402scan with whatever the scrape/local-mirror side had (nothing, for api).
That's not hypothetical: it's exactly what happened once during this
script's own development, wiping the api URL (and flipping `active` to
false) for nine real facilitators in production before it was caught and
fixed by hand. download_db.is_downloaded(neon_url) now guards against
exactly this -- it checks the local mirror was downloaded from this exact
DATABASE_URL (by comparing against a hash recorded at download time, not
just "some download happened at some point"), and forces a fresh download
whenever that doesn't match, rather than silently diffing against
whatever's on disk.

Step 5's server/service ops don't carry that same risk even if the local
mirror were somehow still wrong: apply_server_ops's INSERT for a "new"
server only touches `updated` on an (name) conflict, never api/doc/active,
so mistaking an already-existing server for a new one wastes some work
(and a redundant liveness probe) rather than corrupting anything -- unlike
facilitator.api, which is exactly the field the incident above wiped.

Before any of the above: every required env var, library, and external
tool is checked up front and reported together (preflight.py), rather than
failing with a traceback -- or worse, a partially-applied change -- partway
through a run:
  - required packages (psycopg2, requests, python-dotenv, pyjwt,
    cryptography) via a guarded import at the top of this file and of each
    module it imports (check_supported.py, download_db.py,
    load_facilitators.py) -- missing one prints which one and exits
    immediately, since nothing downstream can work without it anyway.
  - the sibling cli-tool project checked out next to this one (needed by
    load_facilitators.fingerprint_urls, transitively) -- same treatment,
    checked where that import happens.
  - DATABASE_URL (Neon): set, and actually reachable.
  - CDP_API_KEY_ID/CDP_API_KEY_SECRET (check_supported.preflight_checks()):
    needed for step 4 to check Coinbase specifically -- *not* needed for
    step 5's Bazaar discovery scan, which is unauthenticated.
  - the PostgreSQL binaries download_db.py shells out to, and its local
    port being free (download_db.preflight_checks()).

Usage:
    python scripts/update.py

Requires DATABASE_URL (Neon) and CDP_API_KEY_ID/CDP_API_KEY_SECRET, read
from a .env file in the project root (if present) or the real environment.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent

import preflight  # noqa: E402

try:
    from dotenv import load_dotenv
    import psycopg2
    import psycopg2.extras
    import requests  # needed by x402scan_scraper, imported below
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

# Each of these has its own guarded import for its own extra dependencies
# (requests, pyjwt/cryptography via cdp_client, cli-tool's x402tool via
# load_facilitators) -- if any of those are missing, importing it here
# prints a clear message and exits immediately, the same way a missing
# package above does.
import check_supported  # noqa: E402
import download_db  # noqa: E402
import service_classifier  # noqa: E402
import x402scan_scraper  # noqa: E402
from cdp_client import CdpClient  # noqa: E402
from db_setup import ensure_schema  # noqa: E402
from load_facilitators import _link_address, _upsert_facilitator, _upsert_uri, fingerprint_urls  # noqa: E402

OWNER_FACILITATOR = 0
OWNER_SERVER = 1

BAZAAR_PAGE_LIMIT = 200
SERVICE_PROBE_TIMEOUT = 8.0
SERVICE_PROBE_MAX_WORKERS = 10


def _normalize_address(address: str) -> str:
    """Case-insensitive for 0x-prefixed (EVM) addresses (case isn't
    significant for those); case-sensitive otherwise, e.g. Solana base58
    addresses -- same rule cli-tool's combine_facilitators.py uses, so an
    address already in the database under different EVM-address casing
    isn't reported as "new"."""
    return address.lower() if address.lower().startswith("0x") else address


def fetch_local_facilitators(cur) -> dict[str, dict]:
    """name.lower() -> {"id", "name", "api", "active", "api_url", "doc_url",
    "x402scan_url"} for every facilitator currently in the database."""
    cur.execute(
        """
        SELECT f.id, f.name, f.api, f.active,
               api_uri.url AS api_url,
               doc_uri.url AS doc_url,
               x402scan_uri.url AS x402scan_url
        FROM facilitator f
        LEFT JOIN uris api_uri ON api_uri.id = f.api
        LEFT JOIN uris doc_uri ON doc_uri.id = f.doc
        LEFT JOIN uris x402scan_uri ON x402scan_uri.id = f.x402scan
        """
    )
    return {row["name"].lower(): dict(row) for row in cur.fetchall()}


def fetch_local_addresses(cur) -> dict[int, set[str]]:
    """facilitator id -> the set of addresses currently linked to it."""
    cur.execute(
        """
        SELECT la.ref AS facilitator_id, a.address
        FROM linkaddresses la
        JOIN addresses a ON a.id = la.address
        WHERE la.owner = %s
        """,
        (OWNER_FACILITATOR,),
    )
    result: dict[int, set[str]] = {}
    for row in cur.fetchall():
        result.setdefault(row["facilitator_id"], set()).add(row["address"])
    return result


# --- Servers and their services -----------------------------------------
#
# Two sources, merged by hostname (the same real server can show up in
# both, but only x402scan gives it a proper name -- Coinbase's Bazaar
# discovery endpoint is just a flat list of resource URLs with no server
# grouping at all):
#   - x402scan.com's homepage listing (x402scan_scraper.scrape_all()),
#     already used for cli-tool's own `scrape-servers` command.
#   - Coinbase's CDP Bazaar (GET /v2/x402/discovery/resources) --
#     unauthenticated (see cdp_client.py: that path isn't in
#     _AUTHENTICATED_PATHS), so no CDP credentials are needed for this
#     part, unlike check_supported()'s Coinbase-specific check.
#
# Each resource is categorised with scripts/service_classifier.py's
# keyword rules (vendored from cli-tool, same as cdp_client.py/
# x402scan_scraper.py), and, since actually confirming a paid x402
# resource is alive would require making a real payment, "alive" here
# means it responded HTTP 402 Payment Required to a plain, unpaid request
# -- the correct response from a live x402-gated endpoint -- rather than
# skipping a liveness signal entirely.


def _hostname(url: str | None) -> str | None:
    return (urlparse(url or "").hostname or "").lower() or None


def fetch_bazaar_resources() -> list[dict]:
    """Every resource currently in Coinbase's CDP Bazaar, paginated fully
    (offset advances by however many each page actually returned, stopping
    once a page comes back empty or pagination.total is reached) -- the
    same approach cli-tool's `cdp discover-resources-csv2` command uses."""
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
        total = (body.get("pagination") or {}).get("total")
        if not batch or (total is not None and offset >= total):
            break
    return items


def _format_bazaar_price(accepts: list[dict] | None) -> str | None:
    """Bazaar has no single "price" string the way x402scan does -- each
    resource lists one or more {amount, asset, network} payment options
    (`accepts`) instead. Formats those into one human-readable string,
    e.g. "1000 USDC on base; 500 USDC on base-sepolia"."""
    parts = []
    for accept in accepts or []:
        amount, asset, network = accept.get("amount"), accept.get("asset"), accept.get("network")
        if amount and asset:
            parts.append(f"{amount} {asset}" + (f" on {network}" if network else ""))
    return "; ".join(parts) or None


def build_discovered_servers(x402scan_servers: list[dict], bazaar_items: list[dict]) -> dict[str, dict]:
    """hostname -> {"name", "api_url", "doc_url", "addresses": set[str],
    "resources": {path: {"full_url", "description", "price", "tags",
    "method"}}}, merging both sources by hostname so the same real server
    discovered via both becomes one entry. x402scan's own data wins for a
    path both sources report (it has tags and a cleaner price string;
    Bazaar's entry for that same path is simply skipped, not merged in)."""
    servers: dict[str, dict] = {}

    def _server(hostname: str) -> dict:
        return servers.setdefault(
            hostname,
            {"name": None, "api_url": None, "doc_url": None, "addresses": set(), "resources": {}},
        )

    for group in x402scan_servers:
        hostname = _hostname(group.get("api_url"))
        if not hostname:
            continue
        entry = _server(hostname)
        entry["name"] = group.get("name") or entry["name"]
        entry["api_url"] = entry["api_url"] or group.get("api_url")
        entry["doc_url"] = entry["doc_url"] or group.get("doc_url")
        entry["addresses"].update(group.get("addresses") or [])
        for resource in group.get("resources") or []:
            url = resource.get("url")
            path = urlparse(url or "").path
            if not path:
                continue
            entry["resources"][path] = {
                "full_url": url,
                "description": resource.get("description"),
                "price": resource.get("price"),
                "tags": resource.get("tags") or [],
                "method": resource.get("method"),
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
            pay_to = accept.get("payTo")
            if pay_to:
                entry["addresses"].add(pay_to)
        if not parsed.path or parsed.path in entry["resources"]:
            continue
        entry["resources"][parsed.path] = {
            "full_url": url,
            "description": item.get("description"),
            "price": _format_bazaar_price(item.get("accepts")),
            "tags": [],
            "method": None,
        }

    for hostname, entry in servers.items():
        entry["name"] = entry["name"] or hostname

    return servers


def fetch_local_servers(cur) -> dict[str, dict]:
    """hostname -> {"id", "name", "active"} for every server currently in
    the database, keyed by its api URL's hostname (falling back to its own
    name, lowercased, for the unlikely case of a server with no api URL --
    so it can still be matched by name rather than treated as new)."""
    cur.execute(
        """
        SELECT s.id, s.name, s.active, api_uri.url AS api_url
        FROM server s
        LEFT JOIN uris api_uri ON api_uri.id = s.api
        """
    )
    result: dict[str, dict] = {}
    for row in cur.fetchall():
        key = _hostname(row["api_url"]) or row["name"].lower()
        result[key] = dict(row)
    return result


def fetch_local_services(cur) -> dict[int, set[str]]:
    """server id -> the set of service paths already recorded for it."""
    cur.execute("SELECT server, path FROM service")
    result: dict[int, set[str]] = {}
    for row in cur.fetchall():
        result.setdefault(row["server"], set()).add(row["path"])
    return result


def fetch_local_server_addresses(cur) -> dict[int, set[str]]:
    """server id -> the set of addresses currently linked to it."""
    cur.execute(
        """
        SELECT la.ref AS server_id, a.address
        FROM linkaddresses la
        JOIN addresses a ON a.id = la.address
        WHERE la.owner = %s
        """,
        (OWNER_SERVER,),
    )
    result: dict[int, set[str]] = {}
    for row in cur.fetchall():
        result.setdefault(row["server_id"], set()).add(row["address"])
    return result


def probe_service_alive(url: str, method: str | None) -> bool:
    """A resource is considered alive if it responds HTTP 402 Payment
    Required to a plain, unpaid request -- the correct response from a
    live x402-gated endpoint. Anything else, including a connection
    failure or timeout, means "not confirmed alive" -- there's no way to
    get further than the 402 without an actual payment."""
    try:
        response = requests.request(method or "GET", url, timeout=SERVICE_PROBE_TIMEOUT)
    except requests.RequestException:
        return False
    return response.status_code == 402


def probe_services(candidates: list[tuple[str, str | None]]) -> dict[str, bool]:
    """full_url -> is_alive, probed concurrently for every (url, method)
    pair in `candidates`."""
    results: dict[str, bool] = {}
    if not candidates:
        return results
    with ThreadPoolExecutor(max_workers=SERVICE_PROBE_MAX_WORKERS) as pool:
        futures = {pool.submit(probe_service_alive, url, method): url for url, method in candidates}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results


def _classify(full_url: str | None, description: str | None, tags: list[str]) -> str:
    text = service_classifier.resource_text({"url": full_url, "description": description, "tags": tags})
    return service_classifier.classify_resource(text)


def build_server_ops(
    discovered: dict[str, dict],
    local_servers: dict[str, dict],
    local_services: dict[int, set[str]],
    local_server_addresses: dict[int, set[str]],
    probe_results: dict[str, bool],
) -> list[dict]:
    """Diffs the discovered servers/services against the local database --
    same shape and purpose as build_ops, just for server/service/
    server-address ops instead of facilitator ones. A brand new server
    that turns up with at least one live service upgrades an otherwise
    default-inactive row to active; an *existing* server only ever gets
    upgraded the same way (never downgraded) by a newly found live
    service -- its other, already-known services aren't re-probed here,
    so there's never enough information from this diff alone to justify
    marking a previously-active server inactive."""
    ops: list[dict] = []

    for hostname, entry in discovered.items():
        services = [
            {
                "path": path,
                "description": info["description"],
                "price": info["price"],
                "tags": info["tags"],
                "category": _classify(info["full_url"], info["description"], info["tags"]),
                "active": probe_results.get(info["full_url"], False),
            }
            for path, info in entry["resources"].items()
        ]

        existing = local_servers.get(hostname)
        if existing is None:
            ops.append(
                {
                    "type": "new_server",
                    "name": entry["name"],
                    "api_url": entry["api_url"],
                    "doc_url": entry["doc_url"],
                    "addresses": sorted(entry["addresses"]),
                    "services": services,
                }
            )
            continue

        known_paths = local_services.get(existing["id"], set())
        new_services = [s for s in services if s["path"] not in known_paths]
        for service in new_services:
            ops.append({"type": "new_service", "server_name": existing["name"], **service})

        if not existing["active"] and any(s["active"] for s in new_services):
            ops.append({"type": "update_server_active", "name": existing["name"], "active": True})

        known_addresses = local_server_addresses.get(existing["id"], set())
        for address in entry["addresses"]:
            if address not in known_addresses:
                ops.append({"type": "new_server_address", "server_name": existing["name"], "address": address})

    return ops


def apply_server_ops(cur, ops: list[dict], fingerprints: dict[str, dict]) -> None:
    """Replays server/service ops (from build_server_ops) against one
    database connection's cursor -- see apply_ops's docstring; the same
    upsert-by-natural-key approach applies here."""
    for op in ops:
        if op["type"] == "new_server":
            server_active = any(s["active"] for s in op["services"])
            api_id = _upsert_uri(cur, op["api_url"], fingerprints) if op["api_url"] else None
            doc_id = _upsert_uri(cur, op["doc_url"], fingerprints) if op["doc_url"] else None
            cur.execute(
                """
                INSERT INTO server (name, api, doc, active)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET updated = CURRENT_DATE
                RETURNING id
                """,
                (op["name"], api_id, doc_id, server_active),
            )
            server_id = cur.fetchone()[0]
            for address in op["addresses"]:
                address_id = _get_or_create_address(cur, address)
                _link_address(cur, OWNER_SERVER, server_id, address_id)
            for service in op["services"]:
                _insert_service(cur, server_id, service)

        elif op["type"] == "new_service":
            cur.execute("SELECT id FROM server WHERE name = %s", (op["server_name"],))
            server_id = cur.fetchone()[0]
            _insert_service(cur, server_id, op)

        elif op["type"] == "new_server_address":
            cur.execute("SELECT id FROM server WHERE name = %s", (op["server_name"],))
            server_id = cur.fetchone()[0]
            address_id = _get_or_create_address(cur, op["address"])
            _link_address(cur, OWNER_SERVER, server_id, address_id)

        elif op["type"] == "update_server_active":
            cur.execute(
                "UPDATE server SET active = %s, updated = CURRENT_DATE WHERE name = %s",
                (op["active"], op["name"]),
            )

        else:
            raise ValueError(f"unknown op type: {op['type']!r}")


def _insert_service(cur, server_id: int, service: dict) -> None:
    cur.execute(
        """
        INSERT INTO service (server, path, description, price, tags, category, active)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (server, path) DO NOTHING
        """,
        (
            server_id,
            service["path"],
            service["description"],
            service["price"],
            ", ".join(service["tags"]) or None,
            service["category"],
            service["active"],
        ),
    )


def build_ops(
    scraped: list[dict],
    local_facilitators: dict[str, dict],
    local_addresses: dict[int, set[str]],
) -> list[dict]:
    """Diffs x402scan's scrape against the local database and returns the
    list of changes needed to bring both databases up to date with it --
    doesn't touch either database itself (see apply_ops)."""
    ops: list[dict] = []

    for item in scraped:
        name = item.get("name")
        if not name:
            continue
        if item.get("errors"):
            print(f"  note: x402scan scrape for {name!r} had issues: {'; '.join(item['errors'])}")

        existing = local_facilitators.get(name.lower())
        doc_url = item.get("docs_url")
        x402scan_url = item.get("url")
        addresses = item.get("addresses") or []

        if existing is None:
            ops.append(
                {
                    "type": "new_facilitator",
                    "name": name,
                    "doc_url": doc_url,
                    "x402scan_url": x402scan_url,
                    "addresses": list(addresses),
                }
            )
            continue

        if doc_url and doc_url != existing["doc_url"]:
            ops.append({"type": "update_url", "name": existing["name"], "field": "doc", "url": doc_url})
        if x402scan_url and x402scan_url != existing["x402scan_url"]:
            ops.append({"type": "update_url", "name": existing["name"], "field": "x402scan", "url": x402scan_url})

        known = {_normalize_address(a) for a in local_addresses.get(existing["id"], set())}
        for address in addresses:
            if _normalize_address(address) not in known:
                ops.append({"type": "new_address", "name": existing["name"], "address": address})

    return ops


def _get_or_create_address(cur, address: str) -> int:
    """Returns the id of `address`'s row in `addresses`, inserting a new
    one (chains/source unknown, source left as "x402scan") if it doesn't
    exist yet. Deliberately does NOT overwrite an existing row's
    chains/source the way load_facilitators.py's loader does -- that
    loader is refreshing from an authoritative source file and is meant to
    win; this is just noticing a previously-unknown address and shouldn't
    clobber richer data (e.g. a real chain list) some other source already
    recorded for it."""
    cur.execute("SELECT id FROM addresses WHERE address = %s", (address,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO addresses (address, chains, source) VALUES (%s, NULL, %s) RETURNING id",
        (address, "x402scan"),
    )
    return cur.fetchone()[0]


_URL_FIELD_COLUMNS = {"doc": "doc", "x402scan": "x402scan"}


def apply_ops(cur, ops: list[dict], fingerprints: dict[str, dict]) -> None:
    """Replays `ops` (from build_ops) against one database connection's
    cursor. Every op resolves facilitators/addresses/uris by their natural
    key (name/address/url) rather than a numeric id, so this can be called
    once per database (local, then Neon) without the two ever needing to
    agree on row ids."""
    for op in ops:
        if op["type"] == "new_facilitator":
            doc_id = _upsert_uri(cur, op["doc_url"], fingerprints) if op["doc_url"] else None
            x402scan_id = _upsert_uri(cur, op["x402scan_url"], fingerprints) if op["x402scan_url"] else None
            # api is left None: x402scan doesn't report one, and a
            # facilitator with no known api URL is inactive by definition
            # (see compute_active_ops) -- no separate insert needed to make
            # that true.
            facilitator_id = _upsert_facilitator(cur, op["name"], None, doc_id, x402scan_id)
            for address in op["addresses"]:
                address_id = _get_or_create_address(cur, address)
                _link_address(cur, OWNER_FACILITATOR, facilitator_id, address_id)

        elif op["type"] == "update_url":
            uri_id = _upsert_uri(cur, op["url"], fingerprints)
            column = _URL_FIELD_COLUMNS[op["field"]]
            cur.execute(
                f"UPDATE facilitator SET {column} = %s, updated = CURRENT_DATE WHERE name = %s",
                (uri_id, op["name"]),
            )

        elif op["type"] == "new_address":
            address_id = _get_or_create_address(cur, op["address"])
            cur.execute("SELECT id FROM facilitator WHERE name = %s", (op["name"],))
            facilitator_id = cur.fetchone()[0]
            _link_address(cur, OWNER_FACILITATOR, facilitator_id, address_id)

        elif op["type"] == "update_active":
            cur.execute(
                "UPDATE facilitator SET active = %s, updated = CURRENT_DATE WHERE name = %s",
                (op["active"], op["name"]),
            )

        else:
            raise ValueError(f"unknown op type: {op['type']!r}")


def compute_active_ops(cur) -> list[dict]:
    """Re-derives every facilitator's active state (no api URL, or an api
    URL whose /supported doesn't return valid JSON -> inactive; otherwise
    active) via check_supported.check_supported() -- the same function
    check_supported.py itself calls -- and returns an "update_active" op
    for each one that disagrees with what's currently stored."""
    cur.execute(
        """
        SELECT f.name, f.active, api_uri.url AS api_url
        FROM facilitator f
        LEFT JOIN uris api_uri ON api_uri.id = f.api
        """
    )
    rows = cur.fetchall()

    ops: list[dict] = []
    for row in rows:
        name, current_active, api_url = row["name"], row["active"], row["api_url"]
        if not api_url:
            desired_active = False
        else:
            _url_called, is_valid_json, _text = check_supported.check_supported(api_url)
            desired_active = is_valid_json
        if desired_active != current_active:
            ops.append({"type": "update_active", "name": name, "active": desired_active})
    return ops


def _fingerprint_targets(ops: list[dict]) -> set[str]:
    urls: set[str] = set()
    for op in ops:
        if op["type"] == "new_facilitator":
            urls.update(u for u in (op["doc_url"], op["x402scan_url"]) if u)
        elif op["type"] == "update_url":
            urls.add(op["url"])
    return urls


def _server_fingerprint_targets(server_ops: list[dict]) -> set[str]:
    urls: set[str] = set()
    for op in server_ops:
        if op["type"] == "new_server":
            urls.update(u for u in (op["api_url"], op["doc_url"]) if u)
    return urls


def main() -> None:
    neon_url = os.getenv("DATABASE_URL")
    preflight.check_or_exit(
        preflight.require_database_url(neon_url),
        check_supported.preflight_checks,
        download_db.preflight_checks,
    )

    if not download_db.is_downloaded(neon_url):
        print("local database hasn't been downloaded yet (or was downloaded from a different DATABASE_URL) -- downloading now...")
        download_db.download(neon_url)
    else:
        download_db.ensure_server_running()

    print("scraping x402scan.com/facilitators ...")
    scraped = x402scan_scraper.scrape_all_facilitators()
    print(f"scraped {len(scraped)} facilitator(s) from x402scan")

    print("scraping x402scan.com/servers ...")
    x402scan_servers = x402scan_scraper.scrape_all()
    print(f"scraped {len(x402scan_servers)} server(s) from x402scan")

    print("fetching Coinbase CDP Bazaar's discovered resources ...")
    bazaar_items = fetch_bazaar_resources()
    print(f"fetched {len(bazaar_items)} resource(s) from Bazaar")

    discovered_servers = build_discovered_servers(x402scan_servers, bazaar_items)

    local_conn = psycopg2.connect(download_db.local_db_url())
    neon_conn = psycopg2.connect(neon_url)
    try:
        for conn in (local_conn, neon_conn):
            with conn.cursor() as cur:
                ensure_schema(cur)
            conn.commit()

        with local_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            local_facilitators = fetch_local_facilitators(cur)
            local_addresses = fetch_local_addresses(cur)
            local_servers = fetch_local_servers(cur)
            local_services = fetch_local_services(cur)
            local_server_addresses = fetch_local_server_addresses(cur)

        ops = build_ops(scraped, local_facilitators, local_addresses)
        fingerprints = fingerprint_urls(_fingerprint_targets(ops))

        # Probes every discovered resource's URL, not just ones already
        # known to be new -- telling "new" from "already known" cheaply
        # needs the diff build_server_ops does anyway, so it's simpler (and
        # no less correct, just a bit more network traffic for a handful of
        # already-known services) to probe first and let build_server_ops
        # only use the result for the ones that turn out to actually be new.
        print("probing discovered services for liveness ...")
        probe_candidates = [
            (info["full_url"], info["method"])
            for entry in discovered_servers.values()
            for info in entry["resources"].values()
        ]
        probe_results = probe_services(probe_candidates)

        server_ops = build_server_ops(
            discovered_servers, local_servers, local_services, local_server_addresses, probe_results
        )
        server_fingerprints = fingerprint_urls(_server_fingerprint_targets(server_ops))

        for conn in (local_conn, neon_conn):
            with conn.cursor() as cur:
                apply_ops(cur, ops, fingerprints)
                apply_server_ops(cur, server_ops, server_fingerprints)
            conn.commit()

        with local_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            active_ops = compute_active_ops(cur)

        for conn in (local_conn, neon_conn):
            with conn.cursor() as cur:
                apply_ops(cur, active_ops, fingerprints={})
            conn.commit()
    finally:
        local_conn.close()
        neon_conn.close()

    new_facilitators = sum(1 for op in ops if op["type"] == "new_facilitator")
    new_addresses = sum(len(op["addresses"]) for op in ops if op["type"] == "new_facilitator") + sum(
        1 for op in ops if op["type"] == "new_address"
    )
    url_updates = sum(1 for op in ops if op["type"] == "update_url")

    new_servers = sum(1 for op in server_ops if op["type"] == "new_server")
    new_services = sum(len(op["services"]) for op in server_ops if op["type"] == "new_server") + sum(
        1 for op in server_ops if op["type"] == "new_service"
    )
    new_server_addresses = sum(len(op["addresses"]) for op in server_ops if op["type"] == "new_server") + sum(
        1 for op in server_ops if op["type"] == "new_server_address"
    )
    server_active_changes = sum(1 for op in server_ops if op["type"] == "update_server_active")

    print(
        f"{new_facilitators} new facilitator(s), {new_addresses} new address(es), "
        f"{url_updates} URL update(s), {len(active_ops)} active-state change(s)"
    )
    print(
        f"{new_servers} new server(s), {new_services} new service(s), "
        f"{new_server_addresses} new server address(es), {server_active_changes} server active-state change(s)"
    )


if __name__ == "__main__":
    main()
