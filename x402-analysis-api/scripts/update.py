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
     (vendored from cli-tool, same as cdp_client.py/x402scan_scraper.py).
     Existing services aren't touched. Servers/services aren't probed for
     liveness here: new ones are recorded as not active until
     scripts/check_servers.py (run separately) confirms otherwise.
  6. Every change from steps 3-5 is applied to *both* the local database
     and Neon -- computed once (against the local mirror, which is assumed
     to already match Neon, since this script is the only thing that's
     supposed to write to either), then replayed identically against each
     connection so the two never drift apart. Each connection resolves its
     own foreign keys via upsert-by-natural-key (url/address/name), so the
     two databases' own internal row ids never need to match.

This isn't a distributed transaction: the local commit and the Neon commit
are two separate commits, in that order. If the Neon commit fails after the
local one succeeds -- a dropped connection partway through applying a big
batch of ops is the usual way that happens -- the two databases are left
temporarily out of sync: local has everything from this run, Neon has
whatever it managed to commit before failing, which can include *none* of
it (one failed statement rolls back that whole cursor's work, since nothing
was committed yet). The intent is that the next run just re-derives and
re-applies the same still-pending changes to Neon -- but a facilitator or
server that exists in local and not (yet) in Neon breaks that unless the
ops touching it can tolerate the row being missing: a "new_address"/
"new_service"/"new_server_address" op only ever carries the parent
facilitator/server's *name*, on the assumption (true when build_ops/
build_server_ops computed it, against local) that a plain `SELECT ... WHERE
name = %s` will find it on whatever connection applies the op later. If
Neon doesn't have that row yet, that SELECT returns nothing and a bare
`cur.fetchone()[0]` crashes with `TypeError: 'NoneType' object is not
subscriptable` -- which is exactly what happened in practice once local/
Neon had diverged this way. _get_or_create_facilitator/_get_or_create_server
close that gap: each op now carries enough of its parent's fields (doc/
x402scan URL, or api URL + active) to recreate a reasonable version of it
if it's missing on this connection, so the next run genuinely self-heals
instead of crashing. Not something to build real two-phase-commit machinery
around either way -- this is a maintenance script, not a payments system.

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
rather than corrupting anything -- unlike
facilitator.api, which is exactly the field the incident above wiped.

Database connections are opened right before they're needed and closed
right after, never held open across the slow parts in between (URL
fingerprinting, step 4's per-facilitator
/supported checks -- all third-party network calls that can take minutes
for a large scrape). An idle connection sitting open that whole time is
exactly what caused `psycopg2.OperationalError: SSL connection has been
closed unexpectedly` once in practice: Neon (and Postgres connections
generally) can drop one that's been idle too long. compute_active_ops is
split from fetch_facilitators_for_active_check for the same reason -- the
fetch is a fast DB read, the compute is a slow run of network calls and
touches no database itself, so main() closes the connection between them.

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


def _sanitize(value):
    """Strips embedded NUL (0x00) bytes from strings, recursively through
    lists/dicts. Postgres text columns can't store a NUL byte, and
    psycopg2 raises `ValueError: A string literal cannot contain NUL (0x00)
    characters.` the moment one turns up as a query parameter -- which
    x402scan/Bazaar data occasionally has (e.g. a description scraped from
    unsanitized upstream HTML/JSON). Applied once, right after scraping, so
    every downstream consumer (build_ops, build_discovered_servers,
    build_server_ops, ...) only ever sees already-clean strings."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    return value


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
# x402scan_scraper.py). Liveness is scripts/check_servers.py's job, not
# this script's.


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


def _classify(full_url: str | None, description: str | None, tags: list[str]) -> str:
    text = service_classifier.resource_text({"url": full_url, "description": description, "tags": tags})
    return service_classifier.classify_resource(text)


def build_server_ops(
    discovered: dict[str, dict],
    local_servers: dict[str, dict],
    local_services: dict[int, set[str]],
    local_server_addresses: dict[int, set[str]],
) -> list[dict]:
    """Diffs the discovered servers/services against the local database --
    same shape and purpose as build_ops, just for server/service/
    server-address ops instead of facilitator ones. New servers/services
    are recorded as not active; scripts/check_servers.py is what actually
    probes them and sets `active`."""
    ops: list[dict] = []

    for hostname, entry in discovered.items():
        services = [
            {
                "path": path,
                "description": info["description"],
                "price": info["price"],
                "tags": info["tags"],
                "category": _classify(info["full_url"], info["description"], info["tags"]),
                "active": False,
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

        # server_name/server_api_url/server_active ride along on both op
        # types below so apply_server_ops can recreate the server if this
        # connection doesn't have it -- see its docstring for why that can
        # happen even though build_server_ops only ever emits these for a
        # server already found in `local_servers`.
        known_paths = local_services.get(existing["id"], set())
        new_services = [s for s in services if s["path"] not in known_paths]
        for service in new_services:
            ops.append(
                {
                    "type": "new_service",
                    "server_name": existing["name"],
                    "server_api_url": existing["api_url"],
                    "server_active": existing["active"],
                    **service,
                }
            )

        known_addresses = local_server_addresses.get(existing["id"], set())
        for address in entry["addresses"]:
            if address not in known_addresses:
                ops.append(
                    {
                        "type": "new_server_address",
                        "server_name": existing["name"],
                        "server_api_url": existing["api_url"],
                        "server_active": existing["active"],
                        "address": address,
                    }
                )

    return ops


def _get_or_create_server(cur, name: str, api_url, active: bool, fingerprints: dict) -> int:
    """Returns the id of the server named `name` on *this* connection,
    creating it (with whatever api URL is passed in, doc left unset) if it
    doesn't have one yet. Same reasoning as _get_or_create_facilitator: a
    "new_service"/"new_server_address" op's server is only guaranteed to
    exist in the local mirror build_server_ops computed ops against, not
    necessarily on the connection apply_server_ops happens to be replaying
    them against right now (if an earlier run's Neon write failed partway
    through after local's had already committed) -- so fall back to
    creating it here instead of crashing on a bare `cur.fetchone()[0]`."""
    cur.execute("SELECT id FROM server WHERE name = %s", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    api_id = _upsert_uri(cur, api_url, fingerprints) if api_url else None
    cur.execute(
        """
        INSERT INTO server (name, api, active)
        VALUES (%s, %s, %s)
        ON CONFLICT (name) DO UPDATE SET updated = CURRENT_DATE
        RETURNING id
        """,
        (name, api_id, active),
    )
    return cur.fetchone()[0]


def apply_server_ops(cur, ops: list[dict], fingerprints: dict[str, dict]) -> None:
    """Replays server/service ops (from build_server_ops) against one
    database connection's cursor -- see apply_ops's docstring; the same
    upsert-by-natural-key approach applies here."""
    for op in ops:
        if op["type"] == "new_server":
            api_id = _upsert_uri(cur, op["api_url"], fingerprints) if op["api_url"] else None
            doc_id = _upsert_uri(cur, op["doc_url"], fingerprints) if op["doc_url"] else None
            cur.execute(
                """
                INSERT INTO server (name, api, doc, active)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET updated = CURRENT_DATE
                RETURNING id
                """,
                (op["name"], api_id, doc_id, False),
            )
            server_id = cur.fetchone()[0]
            for address in op["addresses"]:
                address_id = _get_or_create_address(cur, address)
                _link_address(cur, OWNER_SERVER, server_id, address_id)
            for service in op["services"]:
                _insert_service(cur, server_id, service)

        elif op["type"] == "new_service":
            server_id = _get_or_create_server(cur, op["server_name"], op["server_api_url"], op["server_active"], fingerprints)
            _insert_service(cur, server_id, op)

        elif op["type"] == "new_server_address":
            server_id = _get_or_create_server(cur, op["server_name"], op["server_api_url"], op["server_active"], fingerprints)
            address_id = _get_or_create_address(cur, op["address"])
            _link_address(cur, OWNER_SERVER, server_id, address_id)

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
                ops.append(
                    {
                        "type": "new_address",
                        "name": existing["name"],
                        "address": address,
                        # Carried along so apply_ops can recreate the
                        # facilitator if this connection doesn't have it --
                        # see apply_ops's docstring for why that can happen.
                        "facilitator_doc_url": existing["doc_url"],
                        "facilitator_x402scan_url": existing["x402scan_url"],
                    }
                )

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


def _get_or_create_facilitator(cur, name: str, doc_url, x402scan_url, fingerprints: dict) -> int:
    """Returns the id of the facilitator named `name` on *this* connection,
    creating it (with whatever doc/x402scan URLs are passed in, api left
    unset) if it doesn't have one yet.

    Normally a "new_address" op's facilitator is guaranteed to already
    exist (build_ops only emits that op for a facilitator already found in
    `local_facilitators`) and a plain SELECT would do. But apply_ops runs
    once per connection, committing local before Neon (see main()'s
    docstring on why this isn't a distributed transaction) -- if an earlier
    run's Neon write failed partway through *after* local's had already
    committed, local can have a facilitator Neon doesn't, and a later run's
    ops would still only ever say "add an address to facilitator X",
    assuming X already exists everywhere. Falling back to creating it here
    lets that situation self-heal on the next run instead of crashing with
    `TypeError: 'NoneType' object is not subscriptable` on a bare
    `cur.fetchone()[0]`."""
    cur.execute("SELECT id FROM facilitator WHERE name = %s", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    doc_id = _upsert_uri(cur, doc_url, fingerprints) if doc_url else None
    x402scan_id = _upsert_uri(cur, x402scan_url, fingerprints) if x402scan_url else None
    return _upsert_facilitator(cur, name, None, doc_id, x402scan_id)


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
            facilitator_id = _get_or_create_facilitator(
                cur, op["name"], op["facilitator_doc_url"], op["facilitator_x402scan_url"], fingerprints
            )
            _link_address(cur, OWNER_FACILITATOR, facilitator_id, address_id)

        elif op["type"] == "update_active":
            cur.execute(
                "UPDATE facilitator SET active = %s, updated = CURRENT_DATE WHERE name = %s",
                (op["active"], op["name"]),
            )

        else:
            raise ValueError(f"unknown op type: {op['type']!r}")


def fetch_facilitators_for_active_check(cur) -> list[dict]:
    """{"name", "active", "api_url"} for every facilitator -- the fast,
    DB-only read half of what compute_active_ops needs. Split out from it
    on purpose: compute_active_ops itself makes a live HTTP request per
    facilitator and can take a while, and running that with a database
    connection open and idle the whole time is exactly what caused
    `psycopg2.OperationalError: SSL connection has been closed
    unexpectedly` (Neon -- and any Postgres server, really -- can drop a
    connection that's been idle too long). Callers should fetch these rows,
    close the connection, *then* call compute_active_ops with the result."""
    cur.execute(
        """
        SELECT f.name, f.active, api_uri.url AS api_url
        FROM facilitator f
        LEFT JOIN uris api_uri ON api_uri.id = f.api
        """
    )
    return [dict(row) for row in cur.fetchall()]


def compute_active_ops(rows: list[dict]) -> list[dict]:
    """Re-derives every facilitator's active state (no api URL, or an api
    URL whose /supported doesn't return valid JSON -> inactive; otherwise
    active) via check_supported.check_supported() -- the same function
    check_supported.py itself calls -- and returns an "update_active" op
    for each one that disagrees with what's currently stored. `rows` comes
    from fetch_facilitators_for_active_check(); this function itself never
    touches the database, only the network -- see that function's
    docstring for why that split matters."""
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
    scraped = _sanitize(x402scan_scraper.scrape_all_facilitators())
    print(f"scraped {len(scraped)} facilitator(s) from x402scan")

    print("scraping x402scan.com/servers ...")
    x402scan_servers = _sanitize(x402scan_scraper.scrape_all())
    print(f"scraped {len(x402scan_servers)} server(s) from x402scan")

    print("fetching Coinbase CDP Bazaar's discovered resources ...")
    bazaar_items = _sanitize(fetch_bazaar_resources())
    print(f"fetched {len(bazaar_items)} resource(s) from Bazaar")

    discovered_servers = build_discovered_servers(x402scan_servers, bazaar_items)

    # Every connection below is opened right before it's needed and closed
    # right after -- never held open across the slow (fingerprinting,
    # per-facilitator /supported checks) phases in
    # between. Those can take minutes for a large scrape, and an idle
    # connection sitting open that whole time is exactly what caused
    # `psycopg2.OperationalError: SSL connection has been closed
    # unexpectedly` -- Neon (and Postgres connections generally) can drop
    # one that's been idle too long.

    local_conn = psycopg2.connect(download_db.local_db_url())
    try:
        with local_conn.cursor() as cur:
            ensure_schema(cur)
        local_conn.commit()

        with local_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            local_facilitators = fetch_local_facilitators(cur)
            local_addresses = fetch_local_addresses(cur)
            local_servers = fetch_local_servers(cur)
            local_services = fetch_local_services(cur)
            local_server_addresses = fetch_local_server_addresses(cur)
    finally:
        local_conn.close()

    # --- Slow computation + third-party network calls only below -- no DB
    # connection open across any of it. ---

    ops = build_ops(scraped, local_facilitators, local_addresses)
    fingerprints = fingerprint_urls(_fingerprint_targets(ops))

    server_ops = build_server_ops(discovered_servers, local_servers, local_services, local_server_addresses)
    server_fingerprints = fingerprint_urls(_server_fingerprint_targets(server_ops))

    # --- Back to short-lived connections: apply everything computed so
    # far, then read the now-current facilitator list for the liveness
    # recheck below (fast), and close again before that recheck's slow
    # per-facilitator network calls start. ---

    local_conn = psycopg2.connect(download_db.local_db_url())
    neon_conn = psycopg2.connect(neon_url)
    try:
        for conn in (local_conn, neon_conn):
            with conn.cursor() as cur:
                ensure_schema(cur)
                apply_ops(cur, ops, fingerprints)
                apply_server_ops(cur, server_ops, server_fingerprints)
            conn.commit()

        with local_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            facilitator_rows = fetch_facilitators_for_active_check(cur)
    finally:
        local_conn.close()
        neon_conn.close()

    active_ops = compute_active_ops(facilitator_rows)

    local_conn = psycopg2.connect(download_db.local_db_url())
    neon_conn = psycopg2.connect(neon_url)
    try:
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

    print(
        f"{new_facilitators} new facilitator(s), {new_addresses} new address(es), "
        f"{url_updates} URL update(s), {len(active_ops)} active-state change(s)"
    )
    print(
        f"{new_servers} new server(s), {new_services} new service(s), "
        f"{new_server_addresses} new server address(es)"
    )


if __name__ == "__main__":
    main()
