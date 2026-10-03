#!/usr/bin/env python3
"""Re-checks the active state of every server and every service it offers,
writing any changes to both the local Postgres mirror and Neon.

Steps:
  1. Make sure the local database exists (download_db.download(), if it
     hasn't been downloaded yet -- or was downloaded from a different
     DATABASE_URL, see update.py's docstring for why that matters) and its
     server is running.
  2. Read every server (name, active, api URL) and every service (server,
     path, active) from the local mirror.
  3. Probe every service: since actually confirming a paid x402 resource
     works would require a real payment, a service is considered active if
     a plain, unpaid request to it gets back HTTP 402 Payment Required --
     the correct response from a live x402-gated endpoint. The HTTP method
     a service expects isn't recorded in the database, so GET is tried
     first and POST only if GET didn't get a 402. A service whose server
     has no api URL can't be probed at all, so it's not active.
  4. A server is active if at least one of its services is. A server with
     no services recorded has nothing to probe, so its stored `active`
     value is left alone.
  5. Wherever that disagrees with what's stored, `active` is updated --
     computed once against the local mirror, then replayed identically
     against both the local mirror and Neon (by server name / server name +
     path, so the two databases' own row ids never need to match), the same
     way update.py applies its changes.

Same as update.py, database connections are only held open for the fast
reads/writes either side of the probing, never across it -- probing can
take minutes, and Neon drops connections that sit idle too long.

Usage:
    python scripts/check_servers.py

Requires DATABASE_URL (Neon), read from a .env file in the project root (if
present) or the real environment.
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
    import requests
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

import download_db  # noqa: E402
from db_setup import ensure_schema  # noqa: E402

SERVICE_PROBE_TIMEOUT = 8.0
SERVICE_PROBE_MAX_WORKERS = 10
PROBE_METHODS = ("GET", "POST")


def fetch_servers(cur) -> list[dict]:
    """{"name", "active", "api_url"} for every server."""
    cur.execute(
        """
        SELECT s.name, s.active, api_uri.url AS api_url
        FROM server s
        LEFT JOIN uris api_uri ON api_uri.id = s.api
        """
    )
    return [dict(row) for row in cur.fetchall()]


def fetch_services(cur) -> list[dict]:
    """{"server_name", "path", "active"} for every service."""
    cur.execute(
        """
        SELECT s.name AS server_name, sv.path, sv.active
        FROM service sv
        JOIN server s ON s.id = sv.server
        """
    )
    return [dict(row) for row in cur.fetchall()]


def service_url(api_url: str | None, path: str) -> str | None:
    """The full URL to probe for a service: its server's api URL's scheme
    and host, plus the service's own path (services are stored by path
    alone -- see update.build_discovered_servers)."""
    parsed = urlparse(api_url or "")
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def probe_service_alive(url: str) -> bool:
    """A resource is considered alive if it responds HTTP 402 Payment
    Required to a plain, unpaid request -- see this file's docstring. Tries
    each of PROBE_METHODS in turn, since which one the service expects
    isn't recorded; a connection failure or timeout ends it early, since a
    different method won't fix an unreachable host."""
    for method in PROBE_METHODS:
        try:
            response = requests.request(method, url, timeout=SERVICE_PROBE_TIMEOUT)
        except requests.RequestException:
            return False
        if response.status_code == 402:
            return True
    return False


def probe_services(urls: set[str]) -> dict[str, bool]:
    """url -> is_alive, probed concurrently."""
    results: dict[str, bool] = {}
    if not urls:
        return results
    with ThreadPoolExecutor(max_workers=SERVICE_PROBE_MAX_WORKERS) as pool:
        futures = {pool.submit(probe_service_alive, url): url for url in urls}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
            print(".", end="", flush=True)
    print()
    return results


def compute_active_ops(servers: list[dict], services: list[dict]) -> list[dict]:
    """Probes every service and returns an op for each service/server whose
    active state disagrees with what's stored. Touches only the network,
    never the database."""
    api_urls = {s["name"]: s["api_url"] for s in servers}
    urls = {
        (sv["server_name"], sv["path"]): service_url(api_urls.get(sv["server_name"]), sv["path"])
        for sv in services
    }
    print(f"probing {len(urls)} service(s) across {len(servers)} server(s) ...")
    probe_results = probe_services({u for u in urls.values() if u})

    ops: list[dict] = []
    server_alive: dict[str, bool] = {}
    for sv in services:
        url = urls[(sv["server_name"], sv["path"])]
        alive = probe_results.get(url, False) if url else False
        server_alive[sv["server_name"]] = server_alive.get(sv["server_name"], False) or alive
        if alive != sv["active"]:
            ops.append({"type": "update_service_active", "server_name": sv["server_name"], "path": sv["path"], "active": alive})

    for server in servers:
        if server["name"] not in server_alive:
            continue
        alive = server_alive[server["name"]]
        if alive != server["active"]:
            ops.append({"type": "update_server_active", "name": server["name"], "active": alive})

    return ops


def apply_ops(cur, ops: list[dict]) -> None:
    """Replays `ops` against one database connection's cursor, resolving
    servers by name (not id) so it works identically on local and Neon."""
    for op in ops:
        if op["type"] == "update_service_active":
            cur.execute(
                """
                UPDATE service SET active = %s, updated = CURRENT_DATE
                WHERE path = %s AND server = (SELECT id FROM server WHERE name = %s)
                """,
                (op["active"], op["path"], op["server_name"]),
            )
        elif op["type"] == "update_server_active":
            cur.execute(
                "UPDATE server SET active = %s, updated = CURRENT_DATE WHERE name = %s",
                (op["active"], op["name"]),
            )
        else:
            raise ValueError(f"unknown op type: {op['type']!r}")


def main() -> None:
    neon_url = os.getenv("DATABASE_URL")
    preflight.check_or_exit(
        preflight.require_database_url(neon_url),
        download_db.preflight_checks,
    )

    if not download_db.is_downloaded(neon_url):
        print("local database hasn't been downloaded yet (or was downloaded from a different DATABASE_URL) -- downloading now...")
        download_db.download(neon_url)
    else:
        download_db.ensure_server_running()

    local_conn = psycopg2.connect(download_db.local_db_url())
    try:
        with local_conn.cursor() as cur:
            ensure_schema(cur)
        local_conn.commit()
        with local_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            servers = fetch_servers(cur)
            services = fetch_services(cur)
    finally:
        local_conn.close()

    ops = compute_active_ops(servers, services)

    local_conn = psycopg2.connect(download_db.local_db_url())
    neon_conn = psycopg2.connect(neon_url)
    try:
        for conn in (local_conn, neon_conn):
            with conn.cursor() as cur:
                ensure_schema(cur)
                apply_ops(cur, ops)
            conn.commit()
    finally:
        local_conn.close()
        neon_conn.close()

    service_changes = [op for op in ops if op["type"] == "update_service_active"]
    server_changes = [op for op in ops if op["type"] == "update_server_active"]
    print(
        f"{len(service_changes)} service active-state change(s) "
        f"({sum(op['active'] for op in service_changes)} now active), "
        f"{len(server_changes)} server active-state change(s) "
        f"({sum(op['active'] for op in server_changes)} now active)"
    )


if __name__ == "__main__":
    main()
