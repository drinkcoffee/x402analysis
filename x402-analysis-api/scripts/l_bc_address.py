#!/usr/bin/env python3
"""Looks up one address with Blockscout's API, paying for the call with the
x402 protocol instead of an API key.

    GET https://api.blockscout.com/{chain_id}/api/v2/addresses/{address}

Called without an API key, Blockscout responds HTTP 402 Payment Required
with an x402 (version 2) payment request -- currently 0.002 USDC on Base,
"exact" scheme. The official x402 SDK signs a USDC transfer authorization
(EIP-3009) for that amount from the paying wallet and retries the request
with it; Blockscout's facilitator settles the transfer on-chain and returns
the address data. The wallet only signs -- it doesn't send a transaction
itself or need ETH for gas -- but it does need enough USDC on Base.

Payment is only ever made on Base (eip155:8453): any payment option on
another network is refused. A payment request above --max-payment is
refused too, so a price change can't silently cost more than expected.

The address data is printed to standard out as JSON; what was paid (and the
settlement transaction) goes to standard error.

Usage:
    python scripts/l_bc_address.py <address> <chain id> [--max-payment 0.01]

e.g. python scripts/l_bc_address.py 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 1

Needs X402_PRIVATE_KEY -- the hex private key of the wallet that pays, read
from a .env file in either this project's root or cli-tool's. Use a
dedicated wallet holding only a small amount of USDC on Base, never one
holding anything else.

Needs the x402 SDK, which isn't in requirements.txt (that file is also what
the deployed API installs, and the SDK pulls in web3):
    pip install -r scripts/requirements-x402.txt
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI_TOOL_ROOT = PROJECT_ROOT.parent / "cli-tool"

try:
    from dotenv import load_dotenv
    from eth_account import Account
    from x402 import x402ClientSync
    from x402.http.clients import x402_requests
    from x402.http.utils import decode_payment_response_header
    from x402.mechanisms.evm.exact.register import register_exact_evm_client
except ImportError as exc:
    print(
        f"missing required package: {exc.name}. Run `pip install -r "
        "scripts/requirements-x402.txt` from x402-analysis-api/, with your "
        "virtualenv active.",
        file=sys.stderr,
    )
    sys.exit(1)

load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(CLI_TOOL_ROOT / ".env")

import os  # noqa: E402

import preflight  # noqa: E402

BLOCKSCOUT_URL = "https://api.blockscout.com/{chain_id}/api/v2/addresses/{address}"
BASE_NETWORK = "eip155:8453"
USDC_DECIMALS = 6
ETH_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
TIMEOUT = 60.0


def only_base(version, requirements):
    """x402 payment policy: drop every payment option not on Base."""
    return [r for r in requirements if r.network == BASE_NETWORK]


def under_cap(max_units: int):
    """x402 payment policy: drop every payment option costing more than
    `max_units` (in the asset's smallest unit)."""

    def policy(version, requirements):
        return [r for r in requirements if int(r.get_amount()) <= max_units]

    return policy


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("address", help="the address to look up (0x + 40 hex digits)")
    parser.add_argument("chain_id", type=int, help="numeric chain id, e.g. 1 for Ethereum, 8453 for Base")
    parser.add_argument(
        "--max-payment",
        type=Decimal,
        default=Decimal("0.01"),
        help="the most to pay for the call, in USDC (default 0.01)",
    )
    args = parser.parse_args()

    if not ETH_ADDRESS.match(args.address):
        parser.error(f"not an Ethereum-style address: {args.address}")
    preflight.check_or_exit(
        preflight.require_env(
            "X402_PRIVATE_KEY",
            "set it to the hex private key of a wallet holding USDC on Base, in "
            "x402-analysis-api/.env or cli-tool/.env",
        ),
    )

    account = Account.from_key(os.environ["X402_PRIVATE_KEY"])
    max_units = int(args.max_payment * 10**USDC_DECIMALS)
    client = x402ClientSync()
    register_exact_evm_client(client, account, networks=BASE_NETWORK, policies=[only_base, under_cap(max_units)])
    session = x402_requests(client)

    url = BLOCKSCOUT_URL.format(chain_id=args.chain_id, address=args.address)
    print(f"paying from {account.address}, at most {args.max_payment} USDC on Base", file=sys.stderr)
    try:
        response = session.get(url, timeout=TIMEOUT)
    except Exception as exc:  # noqa: BLE001 -- the SDK raises its own error types for a refused payment
        print(f"request failed: {exc}", file=sys.stderr)
        sys.exit(1)

    payment_header = response.headers.get("PAYMENT-RESPONSE")
    if payment_header:
        settlement = decode_payment_response_header(payment_header)
        print(f"payment settled: {settlement.model_dump_json(exclude_none=True)}", file=sys.stderr)

    if not response.ok:
        print(f"HTTP {response.status_code}: {response.text[:1000]}", file=sys.stderr)
        # A rejected payment comes back as another 402, with the reason in
        # its PAYMENT-REQUIRED header rather than the body.
        required = response.headers.get("PAYMENT-REQUIRED")
        if response.status_code == 402 and required:
            reason = json.loads(base64.b64decode(required)).get("error")
            print(f"payment rejected: {reason} -- check the wallet holds enough USDC on Base", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    main()
