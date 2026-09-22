"""Client for two of Blockscout's Pro API address-scoped endpoints.

https://www.blog.blockscout.com/blockscout-pro-api-postman/
https://docs.blockscout.com/api-reference/addresses/list-token-transfers-involving-a-specific-address-with-filtering-options

    GET https://api.blockscout.com/{chainId}/api/v2/addresses/{address}/transactions?apikey=<key>
    GET https://api.blockscout.com/{chainId}/api/v2/addresses/{address}/token-transfers
        ?apikey=<key>&type=<ERC-20,ERC-721,...>&filter=<to|from>&token=<contract address>

One unified base URL across every Blockscout-indexed chain, selected via a
numeric EVM chainId (the same chain ids used everywhere else in this repo -
see etherscan_client.resolve_chain_id, reused here rather than duplicating
a second chain-name table). Free tier: 100K credits/day, 5 requests/second,
no signup card required; get a key at https://dev.blockscout.com/ (format
proapi_xxxx).

Both endpoints also double as x402-gated resources: hit either with no
recognized apikey and it responds 402 with its own x402 payment-required
envelope (offering to accept an on-chain USDC payment as an alternative to
an API key) - fittingly recursive for a tool built around x402, but this
client always takes the api-key route, never the on-chain-payment one. That
same envelope is how the token-transfers response shape below was
confirmed: Blockscout embeds a full example response in its bazaar-info
`extensions`, decodable from the `Payment-Required` header on a 402.

Pagination on both endpoints is keyset-based: a response's
"next_page_params" (or null when there are no more pages) gets echoed back
as the next request's query params.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional

import requests

BASE_URL = "https://api.blockscout.com"

# Token types accepted by the `type` filter on the token-transfers endpoint.
TOKEN_TYPES = {"ERC-20", "ERC-721", "ERC-1155", "ERC-404", "ERC-7984"}


class BlockscoutClient:
    def __init__(self, api_key: Optional[str] = None, timeout: float = 15.0):
        self.api_key = api_key
        self.timeout = timeout

    def _get_page(
        self, path: str, params: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        query = {k: v for k, v in (params or {}).items() if v is not None}
        if self.api_key:
            query["apikey"] = self.api_key
        r = requests.get(f"{BASE_URL}{path}", params=query, timeout=self.timeout)
        try:
            data = r.json()
        except ValueError as exc:
            raise RuntimeError(f"HTTP {r.status_code}: non-JSON response") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"unexpected response shape: {data}")
        if not r.ok:
            raise RuntimeError(str(data.get("error") or data.get("message") or f"HTTP {r.status_code}"))
        return data

    def _iter_pages(
        self,
        path: str,
        base_params: dict[str, Any],
        max_pages: Optional[int],
    ) -> Iterator[dict[str, Any]]:
        page_params: dict[str, Any] = dict(base_params)
        pages_fetched = 0
        while True:
            data = self._get_page(path, page_params)
            for item in data.get("items") or []:
                yield item
            pages_fetched += 1
            next_params = data.get("next_page_params")
            if not next_params or (max_pages is not None and pages_fetched >= max_pages):
                break
            page_params = {**base_params, **next_params}

    def iter_transactions(
        self, chain_id: int, address: str, max_pages: Optional[int] = 1
    ) -> Iterator[dict[str, Any]]:
        """Yield transactions to/from an address, paging up to max_pages
        times (None = keep going until the API reports no more pages)."""
        path = f"/{chain_id}/api/v2/addresses/{address}/transactions"
        yield from self._iter_pages(path, {}, max_pages)

    def iter_token_transfers(
        self,
        chain_id: int,
        address: str,
        token_types: Optional[list[str]] = None,
        direction: Optional[str] = None,
        token: Optional[str] = None,
        max_pages: Optional[int] = 1,
    ) -> Iterator[dict[str, Any]]:
        """Yield token transfers to/from/involving an address.

        token_types: e.g. ["ERC-20"] (comma-joined into the API's `type`
            filter); omit for all token types.
        direction: "to" or "from" to restrict direction; omit for both.
        token: restrict to transfers of one specific token contract address.
        max_pages: page through up to this many pages of results (50/page),
            None to keep going until the API reports no more pages.
        """
        path = f"/{chain_id}/api/v2/addresses/{address}/token-transfers"
        base_params: dict[str, Any] = {}
        if token_types:
            base_params["type"] = ",".join(token_types)
        if direction:
            base_params["filter"] = direction
        if token:
            base_params["token"] = token
        yield from self._iter_pages(path, base_params, max_pages)
