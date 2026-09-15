"""Extract blockchain addresses out of an x402 /supported response.

The addresses a facilitator controls (or nominates for a scheme) show up in
a few places in the response body:
  - `signers`: a dict of network-wildcard -> list of addresses that may sign
    settlements for that network (e.g. `"eip155:*": ["0x...", ...]`).
  - `kinds[].extra.facilitatorAddress` / `receiverAuthorizer`: the `upto` and
    `batch-settlement` schemes name a specific facilitator/receiver address.
  - `kinds[].extra.feePayer`: the Solana `exact` scheme names a fee-payer
    address.
"""

from __future__ import annotations

from typing import Any


def extract_addresses(supported: dict[str, Any]) -> list[str]:
    """Unique addresses referenced anywhere in a /supported response body,
    sorted ascending (case-insensitively)."""
    found: dict[str, str] = {}  # lowercase -> original casing

    def add(value: Any) -> None:
        if isinstance(value, str) and value:
            found.setdefault(value.lower(), value)

    signers = supported.get("signers")
    if isinstance(signers, dict):
        for addresses in signers.values():
            if isinstance(addresses, list):
                for addr in addresses:
                    add(addr)

    for kind in supported.get("kinds") or []:
        extra = kind.get("extra") if isinstance(kind, dict) else None
        if isinstance(extra, dict):
            for field in ("facilitatorAddress", "receiverAuthorizer", "feePayer"):
                add(extra.get(field))

    return [found[key] for key in sorted(found)]
