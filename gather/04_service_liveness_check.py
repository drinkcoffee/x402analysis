#!/usr/bin/env python3
"""Checks which services and servers appear to be live, and records it:
sets every service's and server's `active` in gather's database, and
writes the results to dataset/server_and_services_liveness.json.

A service is live if an unpaid request to it gets back a valid x402
payment request -- the correct response from a working x402-gated
endpoint (getting past it would need a real payment): HTTP 402 Payment
Required, plus either a PAYMENT-REQUIRED header (x402 version 2) or a JSON
body with `accepts` or `x402Version` (version 1). The method a service
expects isn't recorded, so GET is tried first, then POST.

A server is live if at least one of its services is; a server with no
services isn't.

Each service is called at the full URL recorded for it in
dataset/services.json (written by 02_services.py) -- the database only
keeps its path. A service not in that file is called at its server's API
URL's scheme and host plus its path.

Politeness and speed: at most HOST_CONCURRENCY requests at a time go to any
one host, and MAX_WORKERS in all. A host that has never been connected to,
and has then failed to connect DEAD_HOST_FAILURES times in a row (a
connection error, connection timeout or TLS error), is taken to be down:
its remaining services are marked unreachable without being called. A slow
response (read timeout) doesn't count -- the host is up, just slow -- and
nor does anything on a host that has answered at least once.

Progress is printed every PROGRESS_EVERY services (or PROGRESS_SECONDS):
how many are done, how many are live, and the time taken so far. The
database is only written once every service has been checked, so an
interrupted run (Ctrl-C) changes nothing.

Can be run any number of times: each run updates every server and service
and overwrites dataset/server_and_services_liveness.json.

Usage:
    python gather/04_service_liveness_check.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

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
from gatherlib.common import write_json  # noqa: E402

SERVICES_PATH = DATASET_DIR / "services.json"
OUTPUT_PATH = DATASET_DIR / "server_and_services_liveness.json"

PROBE_METHODS = ("GET", "POST")
TIMEOUT = (8.0, 20.0)  # seconds to connect, seconds to respond
MAX_WORKERS = 32
HOST_CONCURRENCY = 4
DEAD_HOST_FAILURES = 3
MAX_BODY_BYTES = 64 * 1024
PROGRESS_EVERY = 500
PROGRESS_SECONDS = 30.0
USER_AGENT = "x402analysis-gather/1.0 (liveness check; no payment is made)"


def fetch_servers_and_services(cur) -> tuple[list[dict], list[dict]]:
    cur.execute(
        """
        SELECT s.id, s.name, u.url AS api_url
        FROM server s
        LEFT JOIN uris u ON u.id = s.api
        ORDER BY lower(s.name)
        """
    )
    servers = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT id, server, path FROM service ORDER BY server, path")
    services = [dict(row) for row in cur.fetchall()]
    return servers, services


def recorded_urls() -> dict[tuple[str, str], str]:
    """(server name, path) -> the full service URL 02_services.py
    recorded, from dataset/services.json; empty if that file's missing."""
    if not SERVICES_PATH.exists():
        print(f"note: {SERVICES_PATH.name} not found -- calling every service at its server's host + path")
        return {}
    data = json.loads(SERVICES_PATH.read_text())
    return {
        (server["name"], service["path"]): service["url"]
        for server in data.get("servers") or []
        for service in server.get("services") or []
        if service.get("url")
    }


def fallback_url(api_url: str | None, path: str) -> str | None:
    parsed = urlparse(api_url or "")
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def is_x402_payment_request(response: requests.Response) -> bool:
    """HTTP 402 with an x402 payment request: a PAYMENT-REQUIRED header
    (version 2), or a JSON body with `accepts` or `x402Version` (version
    1). Reads at most MAX_BODY_BYTES of the body."""
    if response.status_code != 402:
        return False
    if response.headers.get("PAYMENT-REQUIRED"):
        return True
    body = response.raw.read(MAX_BODY_BYTES, decode_content=True)
    try:
        data = json.loads(body)
    except ValueError:
        return False
    return isinstance(data, dict) and ("accepts" in data or "x402Version" in data)


class HostState:
    """Per-host request limit and dead-host tracking, shared by threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.semaphores: dict[str, threading.Semaphore] = defaultdict(lambda: threading.Semaphore(HOST_CONCURRENCY))
        self.failures: dict[str, int] = defaultdict(int)
        self.answered: set[str] = set()

    def semaphore(self, host: str) -> threading.Semaphore:
        with self.lock:
            return self.semaphores[host]

    def is_dead(self, host: str) -> bool:
        with self.lock:
            return host not in self.answered and self.failures[host] >= DEAD_HOST_FAILURES

    def record_answer(self, host: str) -> None:
        with self.lock:
            self.answered.add(host)
            self.failures[host] = 0

    def record_connect_failure(self, host: str) -> None:
        with self.lock:
            self.failures[host] += 1


def probe(url: str, hosts: HostState) -> dict:
    """{"active", "method", "status", "reason"} for one service URL."""
    host = (urlparse(url).hostname or "").lower()
    if hosts.is_dead(host):
        return {"active": False, "method": None, "status": None, "reason": "host unreachable (skipped)"}

    last_status = None
    with hosts.semaphore(host):
        for method in PROBE_METHODS:
            try:
                response = requests.request(
                    method, url, timeout=TIMEOUT, stream=True, headers={"User-Agent": USER_AGENT}
                )
            except requests.RequestException as exc:
                # ConnectTimeout and SSLError are both ConnectionErrors; a
                # ReadTimeout means the host is up but slow, so doesn't count.
                if isinstance(exc, requests.ConnectionError):
                    hosts.record_connect_failure(host)
                return {"active": False, "method": method, "status": None, "reason": f"request failed: {type(exc).__name__}"}
            hosts.record_answer(host)
            try:
                valid = is_x402_payment_request(response)
            except requests.RequestException:
                valid = False
            finally:
                response.close()
            if valid:
                return {"active": True, "method": method, "status": 402, "reason": "x402 payment request"}
            last_status = response.status_code
    reason = "HTTP 402 but not an x402 payment request" if last_status == 402 else f"HTTP {last_status}"
    return {"active": False, "method": PROBE_METHODS[-1], "status": last_status, "reason": reason}


def probe_all(targets: list[tuple[int, str | None]]) -> dict[int, dict]:
    """service id -> probe result, for (service id, url) pairs."""
    results: dict[int, dict] = {}
    to_probe = []
    for service_id, url in targets:
        if url:
            to_probe.append((service_id, url))
        else:
            results[service_id] = {"active": False, "method": None, "status": None, "reason": "no URL to call"}

    # Interleave hosts (one service from each in turn), so workers aren't
    # all left waiting on one big host's HOST_CONCURRENCY limit.
    by_host: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for service_id, url in to_probe:
        by_host[(urlparse(url).hostname or "").lower()].append((service_id, url))
    queues = list(by_host.values())
    to_probe = [queue[i] for i in range(max(map(len, queues), default=0)) for queue in queues if i < len(queue)]

    total = len(targets)
    hosts = HostState()
    started = last_report = time.monotonic()
    live = 0

    def report() -> None:
        elapsed = time.monotonic() - started
        print(
            f"  {len(results):,}/{total:,} service(s) checked, {live:,} live, "
            f"{int(elapsed // 60)}m{int(elapsed % 60):02d}s",
            flush=True,
        )

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(probe, url, hosts): service_id for service_id, url in to_probe}
        for future in as_completed(futures):
            result = future.result()
            results[futures[future]] = result
            live += result["active"]
            now = time.monotonic()
            if len(results) % PROGRESS_EVERY == 0 or now - last_report >= PROGRESS_SECONDS:
                report()
                last_report = now
    report()
    return results


def store(servers: list[dict], services: list[dict], results: dict[int, dict], server_active: dict[int, bool]) -> None:
    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                UPDATE service SET active = v.active, updated = CURRENT_DATE
                FROM (VALUES %s) AS v (id, active)
                WHERE service.id = v.id
                """,
                [(sv["id"], results[sv["id"]]["active"]) for sv in services],
                page_size=1000,
            )
            psycopg2.extras.execute_values(
                cur,
                """
                UPDATE server SET active = v.active, updated = CURRENT_DATE
                FROM (VALUES %s) AS v (id, active)
                WHERE server.id = v.id
                """,
                [(s["id"], server_active[s["id"]]) for s in servers],
                page_size=1000,
            )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    preflight.check_or_exit(localdb.preflight_checks)
    conn = localdb.connect_existing()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            servers, services = fetch_servers_and_services(cur)
    finally:
        conn.close()
    if not servers:
        print("no servers in the database -- run 02_services.py first.", file=sys.stderr)
        sys.exit(2)

    urls = recorded_urls()
    by_id = {s["id"]: s for s in servers}
    service_urls = {
        sv["id"]: urls.get((by_id[sv["server"]]["name"], sv["path"]))
        or fallback_url(by_id[sv["server"]]["api_url"], sv["path"])
        for sv in services
    }
    hosts = {(urlparse(u).hostname or "").lower() for u in service_urls.values() if u}
    print(f"checking {len(services):,} service(s) on {len(hosts):,} host(s), for {len(servers):,} server(s)")

    results = probe_all(list(service_urls.items()))

    server_active = {s["id"]: False for s in servers}
    for sv in services:
        server_active[sv["server"]] |= results[sv["id"]]["active"]

    store(servers, services, results, server_active)

    services_by_server: dict[int, list[dict]] = defaultdict(list)
    for sv in services:
        services_by_server[sv["server"]].append({"path": sv["path"], "url": service_urls[sv["id"]], **results[sv["id"]]})
    write_json(
        OUTPUT_PATH,
        {
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "servers": [
                {
                    "name": s["name"],
                    "api_url": s["api_url"],
                    "active": server_active[s["id"]],
                    "services": services_by_server[s["id"]],
                }
                for s in servers
            ],
        },
    )

    live_services = sum(1 for r in results.values() if r["active"])
    live_servers = sum(server_active.values())
    print(
        f"{live_services:,} of {len(services):,} service(s) and {live_servers:,} of {len(servers):,} "
        f"server(s) live; wrote {OUTPUT_PATH.relative_to(GATHER_ROOT)}"
    )


if __name__ == "__main__":
    main()
