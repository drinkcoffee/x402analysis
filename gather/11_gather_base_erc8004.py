#!/usr/bin/env python3
"""Gathers the AI agents registered on Base under ERC-8004 (Trustless
Agents) into gather's database (erc8004_agents) and
dataset/erc8004-agents-base.json -- the agents an explorer like
https://8004agents.ai/base/agent/000002 shows.

Reads them from where those explorers do: the ERC-8004 identity registry
on Base (IDENTITY_REGISTRY), where each agent is an NFT numbered from 1 up.
For each agent id, through Base's JSON-RPC API (its free public endpoint
by default; set BASE_RPC_URL in gather/.env to use another):
  - ownerOf: the address that owns the agent.
  - getAgentWallet: the wallet the registry has verified for the agent
    (the wallet signs to prove it), if one is set.
  - tokenURI: where the agent's registration file is -- inline as a
    data: URI, or at an https, ipfs:// or ar:// URL, which is fetched.
From the registration file: name, description, x402Support, and the
endpoint of each service named "web", "MCP" or "agentWallet" (older files
call the list "endpoints" rather than "services"). The file itself is
stored too, unless it's over MAX_STORED_REGISTRATION bytes.

Agents whose file can't be fetched or read (a dead host, not JSON, no
URI) are stored anyway, with `error` saying why; --retry-failed tries
those again. The registry's ids have no gaps, so ids are tried in order
until END_AFTER_MISSING in a row don't exist.

Calls to Base are batched, at most BASE_RPC_RPS a second in all (default
18; the public endpoint allows 25), a second's worth to a batch (up to
MAX_AGENTS_PER_RPC_BATCH agents). Calls that are rate limited are sent
again, and failures retried with a growing wait. Registration files are fetched FILE_WORKERS at a
time (at most PER_HOST at once from any one host); a URI pointing at
this machine or a private network isn't followed, since anyone can
register an agent with any URI.

Resumable: agents are committed to the database CHUNK at a time, as
they're gathered. Running this again skips agents already stored (so it
carries on where an earlier run stopped, and then picks up agents
registered since), and rewrites dataset/erc8004-agents-base.json from
everything in the database at the end. Progress: a line per CHUNK agents.

Usage:
    python gather/11_gather_base_erc8004.py [--retry-failed]
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import socket
import sys
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote_to_bytes, urljoin, urlsplit

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
from gatherlib.common import ETH_ADDRESS, write_json  # noqa: E402

OUTPUT_PATH = DATASET_DIR / "erc8004-agents-base.json"
CHAIN = "base"
IDENTITY_REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
PUBLIC_BASE_RPC = "https://mainnet.base.org"
DEFAULT_RPS = 18.0  # the public endpoint allows 25 calls a second
ID_WIDTH = 6  # agent ids are shown zero-padded, e.g. 000002

# Function selectors (the first 4 bytes of the keccak-256 of the signature).
OWNER_OF = "0x6352211e"  # ownerOf(uint256)
TOKEN_URI = "0xc87b56dd"  # tokenURI(uint256)
GET_AGENT_WALLET = "0x00339509"  # getAgentWallet(uint256)
NONEXISTENT_TOKEN = "0x7e273289"  # ERC721NonexistentToken(uint256), the revert for an id that doesn't exist

CHUNK = 100
CALLS_PER_AGENT = 3  # ownerOf, tokenURI, getAgentWallet
MAX_AGENTS_PER_RPC_BATCH = 25
END_AFTER_MISSING = 500
RPC_TIMEOUT = 60.0
RPC_RETRY_WAITS = (2, 5, 10, 30, 60, 120, 300)
RPC_PARTIAL_RETRY_WAIT = 1.0  # when only some of a batch's calls were rate limited

FILE_WORKERS = 16
PER_HOST = 4
FILE_TIMEOUT = 20.0
MAX_FILE_BYTES = 1_000_000
MAX_STORED_REGISTRATION = 100_000
MAX_REDIRECTS = 5
IPFS_GATEWAY = "https://ipfs.io/ipfs/"
ARWEAVE_GATEWAY = "https://arweave.net/"
MAX_PENDING_CHUNKS = 4
SEPARATOR = "; "
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)",
    "Accept": "application/json, */*;q=0.5",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gather the ERC-8004 AI agents registered on Base.")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="also try again for stored agents whose registration file couldn't be fetched or read",
    )
    return parser.parse_args()


def _env_rps() -> float:
    try:
        rps = float(os.getenv("BASE_RPC_RPS") or DEFAULT_RPS)
    except ValueError:
        rps = 0
    if rps <= 0:
        print("BASE_RPC_RPS in gather/.env must be a number more than 0.", file=sys.stderr)
        sys.exit(2)
    return rps


# --- Base RPC -------------------------------------------------------------


class BaseRpc:
    """Batched eth_call against the registry, spaced to at most `rps` calls
    a second. Each batch is about a second's worth of calls: the public
    endpoint counts every call in a batch against its limit, so a batch of
    more than that is refused outright. Errors never show the URL, which
    may carry an API key."""

    def __init__(self, url: str, rps: float) -> None:
        self.url = url
        self.interval = 1.0 / rps
        self.agents_per_batch = max(1, min(MAX_AGENTS_PER_RPC_BATCH, int(rps) // CALLS_PER_AGENT))
        self.next_time = time.monotonic()
        self.session = requests.Session()

    def _pace(self, calls: int) -> None:
        now = time.monotonic()
        if self.next_time > now:
            time.sleep(self.next_time - now)
        self.next_time = max(now, self.next_time) + calls * self.interval

    def calls(self, datas: list[str]) -> list[tuple[str | None, dict | None]]:
        """(result, error) for an eth_call of each of `datas` to the
        registry, in order. An error is the call's own (e.g. a revert).
        Calls that were rate limited, or missing from the reply, are sent
        again -- after a second if others in the batch got through, or
        after each of RPC_RETRY_WAITS while none do (or the request
        fails)."""
        answers: dict[int, tuple[str | None, dict | None]] = {}
        failures = 0
        problem = ""
        while True:
            todo = [i for i in range(len(datas)) if i not in answers]
            if not todo:
                return [answers[i] for i in range(len(datas))]
            batch = [
                {"jsonrpc": "2.0", "id": i, "method": "eth_call", "params": [{"to": IDENTITY_REGISTRY, "data": datas[i]}, "latest"]}
                for i in todo
            ]
            self._pace(len(batch))
            got = 0
            try:
                response = self.session.post(self.url, json=batch, timeout=RPC_TIMEOUT)
            except requests.RequestException as exc:
                problem = type(exc).__name__  # not str(exc): it includes the URL
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    problem = f"HTTP {response.status_code}"
                elif not response.ok:
                    raise RuntimeError(f"Base RPC: HTTP {response.status_code}: {response.text[:200]}")
                else:
                    body = response.json()
                    if not isinstance(body, list):  # the whole batch was refused
                        problem = f"error {(body.get('error') or {}).get('message') or body}"
                    else:
                        problem = "rate limited"
                        for item in body:
                            i = item.get("id")
                            if i in todo and i not in answers and not _rate_limited(item.get("error")):
                                answers[i] = (item.get("result"), item.get("error"))
                                got += 1
            if got:
                if len(answers) < len(datas):
                    time.sleep(RPC_PARTIAL_RETRY_WAIT)
                continue
            if failures == len(RPC_RETRY_WAITS):
                raise RuntimeError(f"Base RPC: still failing after {failures} retries ({problem})")
            wait = RPC_RETRY_WAITS[failures]
            failures += 1
            print(f"\n  Base RPC: {problem} -- retrying in {wait}s", flush=True)
            time.sleep(wait)


def _rate_limited(error: dict | None) -> bool:
    if not error:
        return False
    message = str(error.get("message") or "").lower()
    return error.get("code") in (429, -32005, -32007) or "limit" in message or "too many" in message


def _uint(n: int) -> str:
    return f"{n:064x}"


def _address(result: str | None) -> str | None:
    if not result or len(result) < 66:
        return None
    address = "0x" + result[-40:].lower()
    return None if int(address, 16) == 0 else address


def _string(result: str | None) -> str | None:
    """An ABI-encoded string return value."""
    if not result or len(result) < 130:
        return None
    data = bytes.fromhex(result[2:])
    offset = int.from_bytes(data[:32], "big")
    length = int.from_bytes(data[offset : offset + 32], "big")
    return data[offset + 32 : offset + 32 + length].decode("utf-8", "replace")


def _nonexistent(error: dict | None) -> bool:
    return bool(error) and str(error.get("data") or "").startswith(NONEXISTENT_TOKEN)


def read_agents(rpc: BaseRpc, agent_ids: list[int]) -> dict[int, dict | None]:
    """Agent id -> {"owner", "agent_wallet", "agent_uri"}, or None if the
    id doesn't exist."""
    found: dict[int, dict | None] = {}
    for i in range(0, len(agent_ids), rpc.agents_per_batch):
        ids = agent_ids[i : i + rpc.agents_per_batch]
        datas = [selector + _uint(n) for n in ids for selector in (OWNER_OF, TOKEN_URI, GET_AGENT_WALLET)]
        results = rpc.calls(datas)
        for j, n in enumerate(ids):
            (owner, owner_error), (uri, uri_error), (wallet, _wallet_error) = results[3 * j : 3 * j + 3]
            if _nonexistent(owner_error) or (owner_error and not owner):
                if not _nonexistent(owner_error):
                    raise RuntimeError(f"ownerOf({n}): {owner_error.get('message')}")
                found[n] = None
                continue
            found[n] = {
                "owner": _address(owner),
                "agent_wallet": _address(wallet),
                "agent_uri": (_string(uri) or "").strip() or None,
                "uri_error": uri_error.get("message") if uri_error else None,
            }
    return found


# --- Registration files ---------------------------------------------------


class FileError(Exception):
    pass


_host_slots: dict[str, threading.Semaphore] = {}
_host_slots_lock = threading.Lock()
_thread_local = threading.local()


def _session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
        _thread_local.session.headers.update(HEADERS)
    return _thread_local.session


def _host_slot(host: str) -> threading.Semaphore:
    with _host_slots_lock:
        return _host_slots.setdefault(host, threading.Semaphore(PER_HOST))


def _check_public(host: str) -> None:
    """Refuses a host that is, or resolves to, this machine or a private
    network address."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise FileError(f"can't resolve host {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise FileError(f"host {host} is a private or local address -- not fetched")


def _http_get(url: str) -> bytes:
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise FileError(f"unsupported URL: {url[:200]}")
        _check_public(parts.hostname)
        with _host_slot(parts.hostname):
            try:
                with _session().get(url, timeout=FILE_TIMEOUT, stream=True, allow_redirects=False) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers.get("Location") or "")
                        continue
                    if not response.ok:
                        raise FileError(f"HTTP {response.status_code}")
                    content = b""
                    for piece in response.iter_content(65536):
                        content += piece
                        if len(content) > MAX_FILE_BYTES:
                            raise FileError(f"bigger than {MAX_FILE_BYTES:,} bytes")
                    return content
            except requests.RequestException as exc:
                raise FileError(type(exc).__name__)
    raise FileError("too many redirects")


def _data_uri(uri: str) -> bytes:
    head, _, body = uri.partition(",")
    if ";base64" in head.lower():
        try:
            return base64.b64decode(body + "=" * (-len(body) % 4))
        except ValueError:
            raise FileError("data: URI isn't valid base64")
    return unquote_to_bytes(body)


def fetch_registration(uri: str | None) -> dict:
    """The registration file at `uri`, parsed; FileError if it can't be
    fetched or isn't a JSON object."""
    if not uri:
        raise FileError("no URI")
    if uri.startswith("data:"):
        content = _data_uri(uri)
    elif uri.startswith("{"):  # the JSON itself, given as the URI
        content = uri.encode()
    elif uri.startswith("ipfs://"):
        path = uri[len("ipfs://") :]
        content = _http_get(IPFS_GATEWAY + (path[len("ipfs/") :] if path.startswith("ipfs/") else path))
    elif uri.startswith("ar://"):
        content = _http_get(ARWEAVE_GATEWAY + uri[len("ar://") :])
    else:
        content = _http_get(uri)
    try:
        doc = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise FileError("not JSON")
    if not isinstance(doc, dict):
        raise FileError("not a JSON object")
    return doc


def _text(value) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text.replace("\x00", "").strip() or None


def _flag(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _endpoint(value) -> str | None:
    text = _text(value)
    if text and ETH_ADDRESS.match(text):
        return text.lower()
    if text and text.startswith("eip155:") and ETH_ADDRESS.match(text.rsplit(":", 1)[-1]):
        return text.lower()  # CAIP-10, e.g. eip155:8453:0x...
    return text


def summarize(doc: dict) -> dict:
    """The fields wanted from a registration file."""
    endpoints: dict[str, list[str]] = {"web": [], "mcp": [], "agentwallet": []}
    services = doc.get("services")
    if not isinstance(services, list):
        services = doc.get("endpoints")
    for service in services if isinstance(services, list) else []:
        if not isinstance(service, dict):
            continue
        kind = str(service.get("name") or service.get("type") or "").strip().lower()
        endpoint = _endpoint(service.get("endpoint") or service.get("url"))
        if kind in endpoints and endpoint and endpoint not in endpoints[kind]:
            endpoints[kind].append(endpoint)
    x402 = None
    for key in ("x402Support", "x402support", "x402Supported", "x402_support"):
        if key in doc:
            x402 = _flag(doc[key])
            break
    return {
        "name": _text(doc.get("name")),
        "description": _text(doc.get("description")),
        "x402_support": x402,
        "web": endpoints["web"],
        "mcp": endpoints["mcp"],
        "wallet_endpoints": endpoints["agentwallet"],
    }


def gather_file(agent: dict) -> dict:
    """`agent` with what its registration file says added, or the error
    that stopped it being read."""
    empty = {"name": None, "description": None, "x402_support": None, "web": [], "mcp": [], "wallet_endpoints": []}
    if agent.get("uri_error"):
        return {**agent, **empty, "registration": None, "error": f"tokenURI: {agent['uri_error']}"}
    try:
        doc = fetch_registration(agent["agent_uri"])
    except FileError as exc:
        return {**agent, **empty, "registration": None, "error": str(exc)}
    except Exception as exc:  # anything unexpected in someone else's file
        return {**agent, **empty, "registration": None, "error": f"{type(exc).__name__}: {exc}"[:300]}
    raw = json.dumps(doc, ensure_ascii=False).replace("\\u0000", "")
    stored = json.loads(raw) if len(raw) <= MAX_STORED_REGISTRATION else None
    return {**agent, **summarize(doc), "registration": stored, "error": None}


# --- Storing --------------------------------------------------------------


def store(cur, agents: list[dict]) -> None:
    def joined(values: list[str]) -> str | None:
        return SEPARATOR.join(values) or None

    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO erc8004_agents (chain, agent_id, owner, agent_wallet, agent_uri, name, description,
                                    x402_support, web_endpoints, mcp_endpoints, wallet_endpoints, registration, error)
        VALUES %s
        ON CONFLICT (chain, agent_id) DO UPDATE SET
            owner = EXCLUDED.owner, agent_wallet = EXCLUDED.agent_wallet, agent_uri = EXCLUDED.agent_uri,
            name = EXCLUDED.name, description = EXCLUDED.description, x402_support = EXCLUDED.x402_support,
            web_endpoints = EXCLUDED.web_endpoints, mcp_endpoints = EXCLUDED.mcp_endpoints,
            wallet_endpoints = EXCLUDED.wallet_endpoints, registration = EXCLUDED.registration,
            error = EXCLUDED.error, updated = CURRENT_DATE
        """,
        [
            (
                CHAIN,
                a["agent_id"],
                a["owner"],
                a["agent_wallet"],
                _text(a["agent_uri"]),
                a["name"],
                a["description"],
                a["x402_support"],
                joined(a["web"]),
                joined(a["mcp"]),
                joined(a["wallet_endpoints"]),
                psycopg2.extras.Json(a["registration"]) if a["registration"] is not None else None,
                a["error"],
            )
            for a in agents
        ],
        page_size=500,
    )


def stored_ids(cur, retry_failed: bool) -> set[int]:
    """Ids of agents already stored that don't need gathering again."""
    condition = " AND error IS NULL" if retry_failed else ""
    cur.execute(f"SELECT agent_id FROM erc8004_agents WHERE chain = %s{condition}", (CHAIN,))
    return {row[0] for row in cur.fetchall()}


def write_output(cur) -> dict:
    cur.execute(
        """
        SELECT agent_id, owner, agent_wallet, agent_uri, name, description, x402_support,
               web_endpoints, mcp_endpoints, wallet_endpoints, error
        FROM erc8004_agents WHERE chain = %s ORDER BY agent_id
        """,
        (CHAIN,),
    )

    def split(value: str | None) -> list[str]:
        return value.split(SEPARATOR) if value else []

    agents = [
        {
            "agent_id": str(agent_id).zfill(ID_WIDTH),
            "owner": owner,
            "agent_wallet": agent_wallet,
            "name": name,
            "description": description,
            "x402_support": x402,
            "web_endpoints": split(web),
            "mcp_endpoints": split(mcp),
            "agent_wallet_endpoints": split(wallet),
            "agent_uri": uri,
            **({"error": error} if error else {}),
        }
        for agent_id, owner, agent_wallet, uri, name, description, x402, web, mcp, wallet, error in cur.fetchall()
    ]
    output = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chain": CHAIN,
        "identity_registry": IDENTITY_REGISTRY,
        "agent_count": len(agents),
        "registration_unreadable": sum(1 for a in agents if "error" in a),
        "x402_supported": sum(1 for a in agents if a["x402_support"]),
        "agents": agents,
    }
    write_json(OUTPUT_PATH, output)
    return output


# --- Main -----------------------------------------------------------------


def main() -> None:
    args = parse_args()
    url = (os.getenv("BASE_RPC_URL") or "").strip() or PUBLIC_BASE_RPC
    rpc = BaseRpc(url, _env_rps())
    rpc_name = "Base public RPC" if url == PUBLIC_BASE_RPC else urlsplit(url).hostname
    preflight.check_or_exit(localdb.preflight_checks)

    conn = localdb.connect_existing()
    with conn.cursor() as cur:
        localdb.ensure_schema(cur)
        done = stored_ids(cur, args.retry_failed)
    conn.commit()
    print(
        f"gathering ERC-8004 agents from Base's identity registry ({IDENTITY_REGISTRY}) through {rpc_name}"
        + (f"; {len(done):,} already stored, skipped" if done else "")
    )

    started = time.monotonic()
    gathered = failed = 0
    missing_in_a_row = 0
    next_id = 1
    pending: deque[tuple[int, int, list[Future]]] = deque()

    def write_ready(keep: int) -> None:
        """Writes finished chunks, oldest first -- waiting for the oldest
        while more than `keep` are still pending."""
        nonlocal gathered, failed
        while pending and (len(pending) > keep or all(f.done() for f in pending[0][2])):
            first, last, futures = pending.popleft()
            agents = [f.result() for f in futures]
            with conn.cursor() as cur:
                store(cur, agents)
            conn.commit()
            gathered += len(agents)
            errors = sum(1 for a in agents if a["error"])
            failed += errors
            rate = gathered / max(time.monotonic() - started, 1e-6)
            print(
                f"[agents {first:,}-{last:,}] {len(agents):,} stored, {errors:,} registration file(s) unreadable "
                f"({gathered:,} this run, {rate:.1f}/s)",
                flush=True,
            )

    pool = ThreadPoolExecutor(max_workers=FILE_WORKERS)
    try:
        while missing_in_a_row < END_AFTER_MISSING:
            ids = []
            while len(ids) < CHUNK:
                if next_id not in done:
                    ids.append(next_id)
                next_id += 1
            agents = read_agents(rpc, ids)
            futures = []
            for n in ids:
                agent = agents[n]
                if agent is None:
                    missing_in_a_row += 1
                    continue
                missing_in_a_row = 0
                futures.append(pool.submit(gather_file, {"agent_id": n, **agent}))
            if futures:
                pending.append((ids[0], ids[-1], futures))
            write_ready(keep=MAX_PENDING_CHUNKS)
        write_ready(keep=0)
        pool.shutdown()
        with conn.cursor() as cur:
            output = write_output(cur)
        conn.commit()
    except KeyboardInterrupt:
        try:
            write_ready(keep=len(pending))  # store the chunks already finished, without waiting for the rest
        except (KeyboardInterrupt, Exception):
            pass
        print("\ninterrupted -- agents stored so far are kept; run this again to carry on.", file=sys.stderr)
        os._exit(130)  # don't wait for queued file fetches
    except RuntimeError as exc:
        print(f"\nstopping: {exc} -- agents stored so far are kept; run this again to carry on.", file=sys.stderr)
        os._exit(1)
    finally:
        conn.close()

    print(
        f"{output['agent_count']:,} agent(s) on Base ({gathered:,} gathered this run), "
        f"{output['x402_supported']:,} saying they support x402, {output['registration_unreadable']:,} with a "
        f"registration file that couldn't be read; wrote {OUTPUT_PATH.relative_to(GATHER_ROOT)}"
    )
    if failed:
        print("to try the unreadable registration files again: run this with --retry-failed", file=sys.stderr)


if __name__ == "__main__":
    main()
