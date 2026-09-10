"""Client for the generic x402 facilitator HTTP contract.

This is the plain protocol used by the x402 Python/TypeScript SDKs when a
resource server talks to a remote facilitator: POST {base_url}/verify,
POST {base_url}/settle, GET {base_url}/supported. Most third-party
facilitators in the registry implement exactly this.
"""

from __future__ import annotations

from typing import Any, Optional

import requests


class GenericFacilitatorClient:
    def __init__(
        self,
        base_url: str,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 30.0,
        verbose: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.timeout = timeout
        self.verbose = verbose

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json", **self.headers}
        if self.verbose:
            print(f"-> {method} {url}", flush=True)
        return requests.request(method, url, headers=headers, timeout=self.timeout, **kwargs)

    def supported(self) -> requests.Response:
        return self._request("GET", "/supported")

    def verify(
        self, x402_version: int, payment_payload: dict[str, Any], payment_requirements: dict[str, Any]
    ) -> requests.Response:
        body = {
            "x402Version": x402_version,
            "paymentPayload": payment_payload,
            "paymentRequirements": payment_requirements,
        }
        return self._request("POST", "/verify", json=body)

    def settle(
        self, x402_version: int, payment_payload: dict[str, Any], payment_requirements: dict[str, Any]
    ) -> requests.Response:
        body = {
            "x402Version": x402_version,
            "paymentPayload": payment_payload,
            "paymentRequirements": payment_requirements,
        }
        return self._request("POST", "/settle", json=body)
