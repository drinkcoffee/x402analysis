"""Client for the Coinbase CDP Platform x402 Facilitator API (v2).

Covers every route under the "x402 Facilitator" tag of the CDP OpenAPI spec:
verify, settle, supported, discovery/{resources,merchant,search,mcp,bundles},
and validate.

Reference:
https://docs.cdp.coinbase.com/api-reference/v2/rest-api/x402-facilitator/x402-facilitator
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode

import requests

from .cdp_auth import build_bearer_token

CDP_HOST = "api.cdp.coinbase.com"
CDP_BASE_URL = f"https://{CDP_HOST}/platform"

# These x402 Facilitator routes require a CDP-signed bearer JWT.
_AUTHENTICATED_PATHS = {"/v2/x402/verify", "/v2/x402/settle", "/v2/x402/supported"}


class CdpClientError(Exception):
    pass


class CdpClient:
    def __init__(
        self,
        key_id: Optional[str] = None,
        key_secret: Optional[str] = None,
        base_url: str = CDP_BASE_URL,
        timeout: float = 30.0,
        verbose: bool = False,
    ):
        self.key_id = key_id
        self.key_secret = key_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.verbose = verbose

    def _auth_header(self, method: str, path: str) -> dict[str, str]:
        if not self.key_id or not self.key_secret:
            raise CdpClientError(
                "This endpoint requires CDP API credentials. Pass "
                "--api-key-id/--api-key-secret or set the CDP_API_KEY_ID / "
                "CDP_API_KEY_SECRET environment variables."
            )
        token = build_bearer_token(self.key_id, self.key_secret, method, CDP_HOST, path)
        return {"Authorization": f"Bearer {token}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> requests.Response:
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json"}
        if path in _AUTHENTICATED_PATHS:
            headers.update(self._auth_header(method, path))
        if self.verbose:
            suffix = f"?{urlencode(params, doseq=True)}" if params else ""
            print(f"-> {method} {url}{suffix}", flush=True)
        return requests.request(
            method, url, params=params, json=json_body, headers=headers, timeout=self.timeout
        )

    # -- Core x402 protocol ------------------------------------------------

    def verify(
        self, x402_version: int, payment_payload: dict[str, Any], payment_requirements: dict[str, Any]
    ) -> requests.Response:
        body = {
            "x402Version": x402_version,
            "paymentPayload": payment_payload,
            "paymentRequirements": payment_requirements,
        }
        return self._request("POST", "/v2/x402/verify", json_body=body)

    def settle(
        self, x402_version: int, payment_payload: dict[str, Any], payment_requirements: dict[str, Any]
    ) -> requests.Response:
        body = {
            "x402Version": x402_version,
            "paymentPayload": payment_payload,
            "paymentRequirements": payment_requirements,
        }
        return self._request("POST", "/v2/x402/settle", json_body=body)

    def supported(self) -> requests.Response:
        return self._request("GET", "/v2/x402/supported")

    # -- Bazaar discovery (unauthenticated) --------------------------------

    def discovery_resources(
        self, type_: Optional[str] = None, limit: Optional[int] = None, offset: Optional[int] = None
    ) -> requests.Response:
        params = {"type": type_, "limit": limit, "offset": offset}
        params = {k: v for k, v in params.items() if v is not None}
        return self._request("GET", "/v2/x402/discovery/resources", params=params)

    def discovery_merchant(
        self, pay_to: str, limit: Optional[int] = None, offset: Optional[int] = None
    ) -> requests.Response:
        params: dict[str, Any] = {"payTo": pay_to}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return self._request("GET", "/v2/x402/discovery/merchant", params=params)

    def discovery_search(self, **kwargs: Any) -> requests.Response:
        params = {k: v for k, v in kwargs.items() if v is not None}
        return self._request("GET", "/v2/x402/discovery/search", params=params)

    def discovery_bundles(self) -> requests.Response:
        return self._request("GET", "/v2/x402/discovery/bundles")

    def discovery_bundle(self, bundle_slug: str) -> requests.Response:
        return self._request("GET", f"/v2/x402/discovery/bundles/{bundle_slug}")

    def discovery_mcp(
        self, method: str, params: Optional[dict[str, Any]] = None, request_id: Any = 1
    ) -> requests.Response:
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            body["params"] = params
        return self._request("POST", "/v2/x402/discovery/mcp", json_body=body)

    def validate(self, resource: str, method: str = "GET") -> requests.Response:
        body = {"resource": resource, "method": method}
        return self._request("POST", "/v2/x402/validate", json_body=body)
