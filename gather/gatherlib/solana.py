"""Solana JSON-RPC helpers shared by 06_find_clients_solana.py and
08_find_funders_solana.py: which RPC endpoint to use, a rate-limited client
for it, and reading transfers out of parsed transactions.

Uses Solana's standard JSON-RPC methods only (getTokenAccountsByOwner,
getSignaturesForAddress, getTransaction), so any RPC endpoint works:
Helius (https://www.helius.dev/) by default, with its key in gather/.env
as HELIUS_API_KEY; any other provider's endpoint (Alchemy, QuickNode,
Triton, ...) with --rpc-url, its URL in gather/.env as SOLANA_RPC_URL; or
Solana's free public endpoint with --public-rpc, which is much more
heavily rate limited.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator
from urllib.parse import urlsplit

import requests

HELIUS_URL = "https://mainnet.helius-rpc.com/?api-key={key}"
PUBLIC_URL = "https://api.mainnet-beta.solana.com"
DEFAULT_HELIUS_RPS = 10.0  # Helius's free plan limit
DEFAULT_PUBLIC_RPS = 3.0  # the public endpoint allows ~4 getTransaction calls a second
DEFAULT_CUSTOM_RPS = 10.0  # roughly what free plans allow; set SOLANA_RPC_RPS for more

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
SOL_DECIMALS = 9
SYSTEM_PROGRAM = "system"
TOKEN_PROGRAM = "spl-token"

SIGNATURES_PER_PAGE = 1000
TIMEOUT = 30.0
MAX_RETRIES = 14  # waits of 1, 2, 4 ... 60s: about 8 minutes in all
MAX_RETRY_WAIT = 60.0
DOT_EVERY = 100
COUNT_EVERY = 2000

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def is_solana_address(address: str) -> bool:
    """Whether `address` is base58 decoding to 32 bytes (a Solana public
    key). Rules out e.g. Tron addresses, which decode to 25."""
    if not 32 <= len(address) <= 44:
        return False
    n = 0
    for char in address:
        digit = BASE58_ALPHABET.find(char)
        if digit < 0:
            return False
        n = n * 58 + digit
    leading_zero_bytes = len(address) - len(address.lstrip("1"))
    return leading_zero_bytes + (n.bit_length() + 7) // 8 == 32


def helius_url() -> str:
    """Helius's RPC URL, with the key from HELIUS_API_KEY; exits with a
    message if that isn't set."""
    key = (os.getenv("HELIUS_API_KEY") or "").strip()
    if not key:
        print(
            "HELIUS_API_KEY isn't set -- add it to gather/.env (a free key from https://www.helius.dev/).",
            file=sys.stderr,
        )
        sys.exit(2)
    return HELIUS_URL.format(key=key)


def _env_rps(name: str, default: float) -> float:
    try:
        rps = float(os.getenv(name) or default)
    except ValueError:
        rps = 0
    if rps <= 0:
        print(f"{name} in gather/.env must be a number more than 0.", file=sys.stderr)
        sys.exit(2)
    return rps


def helius_rps() -> float:
    """Requests a second for Helius: HELIUS_RPS, or DEFAULT_HELIUS_RPS."""
    return _env_rps("HELIUS_RPS", DEFAULT_HELIUS_RPS)


def custom_url() -> str:
    """The RPC URL in SOLANA_RPC_URL; exits with a message if it isn't set
    or isn't an http(s) URL."""
    url = (os.getenv("SOLANA_RPC_URL") or "").strip()
    if not url:
        print(
            "SOLANA_RPC_URL isn't set -- add your RPC provider's Solana mainnet URL (with its key) "
            "to gather/.env to use --rpc-url.",
            file=sys.stderr,
        )
        sys.exit(2)
    if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
        print("SOLANA_RPC_URL in gather/.env must be an http(s) URL.", file=sys.stderr)
        sys.exit(2)
    return url


def add_rpc_arguments(parser: argparse.ArgumentParser) -> None:
    """The options choosing which RPC endpoint to use, and how fast."""
    endpoint = parser.add_mutually_exclusive_group()
    endpoint.add_argument(
        "--public-rpc",
        action="store_true",
        help=f"use Solana's public RPC endpoint ({PUBLIC_URL}) instead of Helius -- no key needed, but much slower",
    )
    endpoint.add_argument(
        "--rpc-url",
        action="store_true",
        help="use the RPC endpoint in SOLANA_RPC_URL (in gather/.env) instead of Helius -- e.g. Alchemy, "
        "QuickNode or Triton. The URL is read from .env, not given here, so its key stays out of your "
        "shell history",
    )
    parser.add_argument(
        "--rps",
        type=float,
        help=f"requests a second (default: HELIUS_RPS or {DEFAULT_HELIUS_RPS:g} for Helius, "
        f"SOLANA_RPC_RPS or {DEFAULT_CUSTOM_RPS:g} for --rpc-url, {DEFAULT_PUBLIC_RPS:g} for --public-rpc)",
    )


def rpc_from_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> tuple["SolanaRpc", str, str]:
    """(client, name, what to check if it doesn't answer) for the endpoint
    the options chose. The name never includes the URL's path or query,
    where providers put the key -- for --rpc-url it's just the host."""
    if args.rps is not None and args.rps <= 0:
        parser.error("--rps must be more than 0")
    if args.public_rpc:
        return SolanaRpc(PUBLIC_URL, args.rps or DEFAULT_PUBLIC_RPS), "Solana public RPC", ""
    if args.rpc_url:
        url = custom_url()
        rps = args.rps or _env_rps("SOLANA_RPC_RPS", DEFAULT_CUSTOM_RPS)
        return SolanaRpc(url, rps), urlsplit(url).hostname, " (check SOLANA_RPC_URL)"
    return SolanaRpc(helius_url(), args.rps or helius_rps()), "Helius", " (check HELIUS_API_KEY)"


# What providers say (in lower case) once a plan's credits are used up:
# Helius, then Alchemy.
USAGE_EXHAUSTED_MESSAGES = ("max usage reached", "monthly capacity limit")


class UsageExhausted(RuntimeError):
    """The RPC provider has stopped serving this key until its plan's
    credits reset -- not worth retrying."""


class SolanaRpc:
    """JSON-RPC calls, shared by threads: at most `rps` requests a second
    in all, retrying rate-limited and failed requests (server errors,
    timeouts, dropped connections) with a growing wait -- for about 8
    minutes, so an outage pauses a run rather than failing it.
    Being rate limited pauses every thread, not just the one that was
    told. Error messages never include the URL, which carries the API
    key."""

    def __init__(self, url: str, rps: float) -> None:
        self.url = url
        self.interval = 1.0 / rps
        self.lock = threading.Lock()
        self.next_slot = 0.0
        self.local = threading.local()

    def _session(self) -> requests.Session:
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
        return self.local.session

    def _wait_turn(self) -> None:
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_slot)
            self.next_slot = slot + self.interval
        if slot > now:
            time.sleep(slot - now)

    def _pause_all(self, seconds: float) -> bool:
        """Holds every thread's next request back `seconds`; False if
        they're already held back that long (another thread paused them)."""
        with self.lock:
            until = time.monotonic() + seconds
            if self.next_slot >= until - 1:
                return False
            self.next_slot = until
            return True

    def call(self, method: str, params: list):
        problem = ""
        for attempt in range(MAX_RETRIES):
            self._wait_turn()
            wait = min(MAX_RETRY_WAIT, 2.0 ** attempt)
            try:
                response = self._session().post(
                    self.url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                # Not str(exc): it includes the URL, and so the API key.
                problem = type(exc).__name__
            else:
                if response.status_code == 429 and any(m in response.text.lower() for m in USAGE_EXHAUSTED_MESSAGES):
                    # The provider's answer once the plan's credits are used
                    # up: it won't change until they reset, so waiting is no
                    # use.
                    raise UsageExhausted(f"{method}: the RPC provider says the plan's usage limit has been reached")
                if response.status_code == 429 or response.status_code >= 500:
                    problem = f"HTTP {response.status_code}"
                    try:
                        wait = max(wait, float(response.headers.get("Retry-After") or 0))
                    except ValueError:
                        pass
                elif not response.ok:
                    raise RuntimeError(f"{method}: HTTP {response.status_code}: {response.text[:200]}")
                else:
                    body = response.json()
                    error = body.get("error")
                    if not error:
                        return body.get("result")
                    message = str(error.get("message") or "")
                    if error.get("code") not in (429, -32429) and "rate limit" not in message.lower():
                        raise RuntimeError(f"{method}: {message} (code {error.get('code')})")
                    problem = f"rate limited ({message})"
            if problem.startswith(("HTTP 429", "rate limited")):
                if self._pause_all(wait):
                    print(f"\n{method}: {problem} -- pausing {wait:.0f}s", flush=True)
            else:
                print(f"\n{method}: {problem} -- retrying in {wait:.0f}s", flush=True)
                time.sleep(wait)
        raise RuntimeError(f"{method}: still failing after {MAX_RETRIES} attempts ({problem})")


def check_answering(rpc: SolanaRpc, name: str) -> None:
    """Exits with a message if the endpoint doesn't answer a simple call."""
    try:
        rpc.call("getSlot", [])
    except RuntimeError as exc:
        print(f"{name} isn't answering: {exc}", file=sys.stderr)
        sys.exit(2)


def usdc_token_accounts(rpc: SolanaRpc, owner: str) -> list[str]:
    """`owner`'s USDC token accounts -- on Solana, tokens are held by
    token accounts a wallet owns, not by the wallet address itself. Only
    current accounts: one that's since been closed isn't found."""
    result = rpc.call("getTokenAccountsByOwner", [owner, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}])
    return [item["pubkey"] for item in (result or {}).get("value") or []]


def get_transaction(rpc: SolanaRpc, signature: str) -> dict | None:
    return rpc.call(
        "getTransaction",
        [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )


class Progress:
    """A "." per DOT_EVERY transactions, and a count every COUNT_EVERY."""

    def __init__(self) -> None:
        self.count = 0

    def tick(self) -> None:
        self.count += 1
        if self.count % COUNT_EVERY == 0:
            print(f"({self.count:,} transactions)", end="", flush=True)
        elif self.count % DOT_EVERY == 0:
            print(".", end="", flush=True)


def transactions(
    rpc: SolanaRpc, pool: ThreadPoolExecutor, address: str, progress: Progress
) -> Iterator[tuple[dict, dict | None]]:
    """(signature entry, parsed transaction) for every successful
    transaction touching `address`, newest first, across all pages with no
    cap -- fetched a page (SIGNATURES_PER_PAGE) at a time, in parallel on
    `pool`. The transaction is None if the endpoint listed it but couldn't
    return it."""
    before = None
    while True:
        options: dict = {"limit": SIGNATURES_PER_PAGE}
        if before:
            options["before"] = before
        page = rpc.call("getSignaturesForAddress", [address, options]) or []
        succeeded = [entry for entry in page if entry.get("err") is None]
        fetched = pool.map(lambda entry: get_transaction(rpc, entry["signature"]), succeeded)
        for entry, tx in zip(succeeded, fetched):
            progress.tick()
            if tx is not None and (tx.get("meta") or {}).get("err") is not None:
                continue
            yield entry, tx
        if len(page) < SIGNATURES_PER_PAGE:
            return
        before = page[-1]["signature"]


def instructions_in_order(tx: dict) -> Iterator[tuple[int, dict, bool]]:
    """(position, instruction, inner) for every instruction in `tx`, in
    the order they ran: each top-level instruction, then the inner ones it
    made (inner=True). Position counts them all, so (signature, position)
    identifies an instruction."""
    inner = {group["index"]: group.get("instructions") or [] for group in (tx.get("meta") or {}).get("innerInstructions") or []}
    position = 0
    for index, instruction in enumerate(tx["transaction"]["message"].get("instructions") or []):
        yield position, instruction, False
        position += 1
        for ran in inner.get(index, []):
            yield position, ran, True
            position += 1


def _parsed(instruction: dict, program: str) -> tuple[str, dict] | None:
    parsed = instruction.get("parsed")
    if instruction.get("program") != program or not isinstance(parsed, dict):
        return None
    return parsed.get("type"), parsed.get("info") or {}


def usdc_transfer_into(instruction: dict, token_account: str) -> tuple[str, int] | None:
    """(authority, raw amount) if `instruction` is an SPL Token transfer of
    USDC into `token_account` (a USDC token account). The authority is the
    wallet that signed the transfer."""
    parsed = _parsed(instruction, TOKEN_PROGRAM)
    if not parsed or parsed[0] not in ("transfer", "transferChecked"):
        return None
    info = parsed[1]
    # A plain transfer doesn't name the mint, but a token account only
    # ever holds one, and token_account is a USDC account.
    if info.get("destination") != token_account or info.get("mint", USDC_MINT) != USDC_MINT:
        return None
    amount = (info.get("tokenAmount") or {}).get("amount") or info.get("amount")
    authority = info.get("authority") or info.get("multisigAuthority")
    if not amount or not authority:
        return None
    return authority, int(amount)


# System program instructions that send SOL, and the info field naming
# the account receiving it.
SOL_SENDING = {
    "transfer": "destination",
    "transferWithSeed": "destination",
    "createAccount": "newAccount",
    "createAccountWithSeed": "newAccount",
}


def sol_transfer_into(instruction: dict, address: str) -> tuple[str, int] | None:
    """(source, lamports) if `instruction` is a System program instruction
    sending SOL to `address`."""
    parsed = _parsed(instruction, SYSTEM_PROGRAM)
    if not parsed or parsed[0] not in SOL_SENDING:
        return None
    info = parsed[1]
    if info.get(SOL_SENDING[parsed[0]]) != address:
        return None
    lamports = int(info.get("lamports") or 0)
    source = info.get("source")
    if lamports <= 0 or not source:
        return None
    return source, lamports
