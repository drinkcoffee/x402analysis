"""Client for ChainQuery's public Sanctions Check API.

https://chainquery.com/products/sanctions-api

    GET https://chainquery.com/api/sanctions/check/<address>

Unauthenticated, no API key, CORS-open. Response shape:
    {"address", "structurally_valid", "sanctioned", "agreement_count",
     "sources": [{"slug", "list", "jurisdiction", "label", "source_ref",
                  "added_at"}, ...],
     "freshness": {<source slug>: <ISO timestamp>, ...},
     "disclaimer"}

ChainQuery's own published limit is 30 requests/hour/IP (sliding window);
a 429 response carries a `Retry-After` header (observed as 3600 seconds).
Callers here are expected to pace their own requests (this tool's
`check-sanctions` command does one per second, per user instruction, which
is far more aggressive than ChainQuery's published limit - expect 429s on
any nontrivial address list). On a 429 this client waits out the server's
Retry-After once and retries; if it still fails, the address comes back
with an error status rather than the caller blocking indefinitely (a
Retry-After can be a full hour).

This is explicitly described by ChainQuery as an educational research
tool, not a compliance product - not a substitute for a contracted
sanctions-screening feed.
"""

from __future__ import annotations

import time
from typing import Any

import requests

BASE_URL = "https://chainquery.com/api/sanctions/check"


class ChainQueryClient:
    def __init__(self, timeout: float = 15.0, max_retries: int = 1):
        self.timeout = timeout
        self.max_retries = max_retries

    def check(self, address: str) -> dict[str, Any]:
        url = f"{BASE_URL}/{address}"
        attempt = 0
        while True:
            try:
                r = requests.get(url, timeout=self.timeout)
            except requests.exceptions.RequestException as exc:
                return {"error": str(exc)}

            if r.status_code == 429:
                if attempt >= self.max_retries:
                    return {"error": f"rate limited (HTTP 429) after {attempt + 1} attempt(s)"}
                retry_after = r.headers.get("Retry-After", "")
                wait = float(retry_after) if retry_after.isdigit() else 60.0
                time.sleep(wait)
                attempt += 1
                continue

            # A structurally-invalid address comes back as HTTP 400, but
            # still with a normal, useful body ({"structurally_valid":
            # false, ...}) - so parse JSON first and only fall back to an
            # HTTP-status-based error when the body isn't the expected
            # shape at all.
            try:
                data = r.json()
            except ValueError:
                return {"error": f"HTTP {r.status_code}: non-JSON response"}
            if isinstance(data, dict) and "sanctioned" in data:
                return data
            return {"error": f"HTTP {r.status_code}: {data}"}

    def describe(self, address: str) -> str:
        """One-word(ish) status for the ADDRESS,STATUS CSV output."""
        data = self.check(address)
        if "error" in data:
            return f"error: {data['error']}"
        if not data.get("structurally_valid", True):
            return "invalid address"
        if data.get("sanctioned"):
            count = data.get("agreement_count")
            if count is not None:
                return f"sanctioned ({count} source{'s' if count != 1 else ''})"
            return "sanctioned"
        return "clear"
