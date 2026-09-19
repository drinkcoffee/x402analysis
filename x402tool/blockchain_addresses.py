"""Extract blockchain addresses out of an x402 /supported response.

The addresses a facilitator controls (or nominates for a scheme) show up in
a few places in the response body:
  - `signers`: a dict of network(-wildcard) -> list of addresses that may
    sign settlements for that network (e.g. `"eip155:*": ["0x...", ...]`).
  - `kinds[].extra.facilitatorAddress` / `receiverAuthorizer`: the `upto` and
    `batch-settlement` schemes name a specific facilitator/receiver address,
    associated with that kind entry's own `network` field.
  - `kinds[].extra.feePayer`: the Solana `exact` scheme names a fee-payer
    address, likewise associated with that kind entry's `network`.
"""

from __future__ import annotations

from typing import Any


def extract_addresses(supported: dict[str, Any]) -> list[str]:
    """Unique addresses referenced anywhere in a /supported response body,
    sorted ascending (case-insensitively)."""
    found: dict[str, str] = {}  # lowercase -> original casing
    for addr, _network in extract_addresses_with_network(supported):
        found.setdefault(addr.lower(), addr)
    return [found[key] for key in sorted(found)]


def extract_addresses_with_network(supported: dict[str, Any]) -> list[tuple[str, str]]:
    """(address, network) pairs referenced anywhere in a /supported response
    body, deduplicated (case-insensitively on the address) but keeping an
    address once per distinct network it's associated with, sorted ascending
    by (address, network)."""
    found: dict[tuple[str, str], tuple[str, str]] = {}  # (addr.lower(), network) -> (addr, network)

    def add(value: Any, network: str) -> None:
        if isinstance(value, str) and value:
            found.setdefault((value.lower(), network), (value, network))

    signers = supported.get("signers")
    if isinstance(signers, dict):
        for network, addresses in signers.items():
            if isinstance(addresses, list):
                for addr in addresses:
                    add(addr, network)

    for kind in supported.get("kinds") or []:
        if not isinstance(kind, dict):
            continue
        network = kind.get("network") or "unknown"
        extra = kind.get("extra")
        if isinstance(extra, dict):
            for field in ("facilitatorAddress", "receiverAuthorizer", "feePayer"):
                add(extra.get(field), network)

    return [found[key] for key in sorted(found)]
