"""Client for Etherscan's unified V2 API "Get Address Funded By" endpoint.

https://docs.etherscan.io/api-reference/endpoint/fundedby

    GET https://api.etherscan.io/v2/api
        ?module=account&action=fundedby&address=<address>&chainid=<id>&apikey=<key>

This traces an EOA's original funding source: the address, transaction, and
block that first sent it value. It's a PRO-tier endpoint (Standard plan and
above), rate-limited to 2 calls/second, and only works for EOAs (not
contract addresses).

Etherscan's V2 API is a single unified endpoint across EVM chains,
selected via `chainid` - so this needs the caller to say which chain the
address is on. Non-EVM chains (Solana, XRPL, NEAR, Stellar, Canton, ...)
aren't supported at all.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import requests

BASE_URL = "https://api.etherscan.io/v2/api"

# Common chain-name aliases -> Etherscan V2 chainid, covering the networks
# this project's facilitator registry already uses names for. Not
# exhaustive - a bare numeric chain id, or an "eip155:<id>" CAIP-2 string,
# also works directly without needing an entry here.
CHAIN_IDS: dict[str, int] = {
    "ethereum": 1,
    "eth": 1,
    "mainnet": 1,
    "sepolia": 11155111,
    "base": 8453,
    "base-sepolia": 84532,
    "polygon": 137,
    "polygon-amoy": 80002,
    "arbitrum": 42161,
    "arbitrum-one": 42161,
    "arbitrum-sepolia": 421614,
    "optimism": 10,
    "optimism-sepolia": 11155420,
    "avalanche": 43114,
    "avalanche-fuji": 43113,
    "celo": 42220,
    "bsc": 56,
    "bnb": 56,
    "linea": 59144,
    "unichain": 130,
    "worldchain": 480,
    "world-chain": 480,
    "monad": 143,
    "robinhood": 4663,
    "robinhood-testnet": 46630,
}


def resolve_chain_id(blockchain: str) -> Optional[int]:
    """A chain id from a bare number, an "eip155:<id>" CAIP-2 string, or a
    known chain name (case-insensitive). None if it can't be resolved (e.g.
    a non-EVM chain like "solana" - Etherscan's API is EVM-only)."""
    value = blockchain.strip()
    if value.isdigit():
        return int(value)
    match = re.match(r"^eip155:(\d+)$", value, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return CHAIN_IDS.get(value.lower())


class EtherscanClient:
    def __init__(self, api_key: Optional[str] = None, timeout: float = 15.0):
        self.api_key = api_key
        self.timeout = timeout

    def funded_by(self, chain_id: int, address: str) -> dict[str, Any]:
        """{"result": {...}} on success, {"error": "..."} on failure."""
        if not self.api_key:
            return {
                "error": "Etherscan API key not configured (--api-key or $ETHERSCAN_API_KEY)"
            }

        params = {
            "module": "account",
            "action": "fundedby",
            "address": address,
            "chainid": chain_id,
            "apikey": self.api_key,
        }
        try:
            r = requests.get(BASE_URL, params=params, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            return {"error": str(exc)}

        try:
            data = r.json()
        except ValueError:
            return {"error": f"HTTP {r.status_code}: non-JSON response"}

        if not isinstance(data, dict):
            return {"error": f"unexpected response shape: {data}"}
        if str(data.get("status")) != "1":
            return {"error": str(data.get("result") or data.get("message") or "request failed")}
        return {"result": data.get("result")}
