"""Client for Blockscout's Pro API "address transactions" endpoint.

https://www.blog.blockscout.com/blockscout-pro-api-postman/

    GET https://api.blockscout.com/{chainId}/api/v2/addresses/{address}/transactions?apikey=<key>

One unified base URL across every Blockscout-indexed chain, selected via a
numeric EVM chainId (the same chain ids used everywhere else in this repo -
see etherscan_client.resolve_chain_id, reused here rather than duplicating
a second chain-name table). Free tier: 100K credits/day, 5 requests/second,
no signup card required; get a key at https://dev.blockscout.com/ (format
proapi_xxxx).

This specific endpoint also doubles as an x402-gated resource: hit it with
no recognized apikey and it responds 402 with its own x402 payment-required
envelope (offering to accept an on-chain USDC payment as an alternative to
an API key) - fittingly recursive for a tool built around x402, but this
client always takes the api-key route, never the on-chain-payment one.

Pagination is keyset-based: a response's "next_page_params" (or null when
there are no more pages) gets echoed back as the next request's query
params.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional

import requests

BASE_URL = "https://api.blockscout.com"


class BlockscoutClient:
    def __init__(self, api_key: Optional[str] = None, timeout: float = 15.0):
        self.api_key = api_key
        self.timeout = timeout

    def _get_page(
        self, chain_id: int, address: str, page_params: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        params = dict(page_params or {})
        if self.api_key:
            params["apikey"] = self.api_key
        url = f"{BASE_URL}/{chain_id}/api/v2/addresses/{address}/transactions"
        r = requests.get(url, params=params, timeout=self.timeout)
        try:
            data = r.json()
        except ValueError as exc:
            raise RuntimeError(f"HTTP {r.status_code}: non-JSON response") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"unexpected response shape: {data}")
        if not r.ok:
            raise RuntimeError(str(data.get("error") or data.get("message") or f"HTTP {r.status_code}"))
        return data

    def iter_transactions(
        self, chain_id: int, address: str, max_pages: Optional[int] = 1
    ) -> Iterator[dict[str, Any]]:
        """Yield transactions, paging up to max_pages times (None = keep
        going until the API reports no more pages)."""
        page_params: Optional[dict[str, Any]] = None
        pages_fetched = 0
        while True:
            data = self._get_page(chain_id, address, page_params)
            for item in data.get("items") or []:
                yield item
            pages_fetched += 1
            next_params = data.get("next_page_params")
            if not next_params or (max_pages is not None and pages_fetched >= max_pages):
                break
            page_params = next_params
