"""Client for Cloudbric Labs' Threat DB "Hacker Wallet" lookup.

https://labs.cloudbric.com/threatdb/view#tab3

There's no published public API for this - the page's "Hacker Wallet" tab is
a jQuery DataTables grid that POSTs search terms to an internal JSON
endpoint. Reverse-engineered by watching the page's own network traffic
while typing an address into its search box:

    POST https://labs.cloudbric.com/threatdb/gethackerwalletlist
    Content-Type: application/x-www-form-urlencoded
    body: draw=1&start=0&length=10
          &columns[0][data]=threat_id
          &columns[1][data]=address
          &columns[2][data]=crypto_currency
          &columns[3][data]=threat_level
          &columns[4][data]=reported_frequency
          &search[value]=<address>

    -> {"draw":1,"recordsTotal":1,"recordsFiltered":1,
        "data":[{"threat_id":"12","address":"0x...","crypto_currency":"ETH",
                 "threat_level":"4","reported_frequency":"1",
                 "register_date":"...","last_activity":"..."}], ...}

`search[value]` is a case-insensitive substring match (potentially against
more than just the address column), so a hit is only treated as real when a
returned row's `address` matches the query exactly (case-insensitively).

`threat_level` is a small integer; the UI labels observed for it (confirmed
by reproducing searches for one address at each level) are 1=Low, 2=Medium,
3=High, 4=Very High. Any other value falls back to "Level <n>" rather than
guessing a label that hasn't been observed.

This page is explicitly unauthenticated and rate-limited, so this client
makes requests one at a time (no concurrency) with a configurable delay
between them and retry-with-backoff on failure/HTTP 429, reusing one
`requests.Session` the way a browser would.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import requests

BASE_URL = "https://labs.cloudbric.com"
LOOKUP_PATH = "/threatdb/gethackerwalletlist"

THREAT_LEVELS = {
    "1": "Low",
    "2": "Medium",
    "3": "High",
    "4": "Very High",
}


class CloudbricThreatDbClient:
    def __init__(self, timeout: float = 15.0, delay: float = 1.0, max_retries: int = 3):
        self.timeout = timeout
        self.delay = delay
        self.max_retries = max_retries
        self.session = requests.Session()

    def _query(self, address: str) -> dict[str, Any]:
        params = {
            "draw": "1",
            "start": "0",
            "length": "10",
            "columns[0][data]": "threat_id",
            "columns[1][data]": "address",
            "columns[2][data]": "crypto_currency",
            "columns[3][data]": "threat_level",
            "columns[4][data]": "reported_frequency",
            "search[value]": address,
        }
        last_error: Optional[str] = None
        for attempt in range(self.max_retries):
            try:
                r = self.session.post(
                    f"{BASE_URL}{LOOKUP_PATH}",
                    data=params,
                    timeout=self.timeout,
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                if r.status_code == 429:
                    last_error = "rate limited (HTTP 429)"
                    time.sleep(self.delay * (attempt + 2))
                    continue
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = str(exc)
                time.sleep(self.delay * (attempt + 1))
        return {"error": last_error or "lookup failed"}

    def lookup(self, address: str) -> dict[str, Any]:
        """Look up one address. Waits `self.delay` seconds first (pacing for
        the site's rate limit), then returns a normalized result dict."""
        time.sleep(self.delay)
        data = self._query(address)
        if "error" in data:
            return {"error": data["error"]}

        rows = data.get("data") or []
        match = next(
            (row for row in rows if str(row.get("address", "")).lower() == address.lower()),
            None,
        )
        if match is None:
            return {"found": False}

        level_code = str(match.get("threat_level"))
        return {
            "found": True,
            "threat_id": match.get("threat_id"),
            "crypto_currency": match.get("crypto_currency"),
            "threat_level": THREAT_LEVELS.get(level_code, f"Level {level_code}"),
            "reported_frequency": match.get("reported_frequency"),
            "register_date": match.get("register_date"),
            "last_activity": match.get("last_activity"),
        }

    def describe(self, address: str) -> str:
        """One-line human-readable summary for the ADDRESS INFO column."""
        result = self.lookup(address)
        if "error" in result:
            return f"Cloudbric ThreatDB lookup failed: {result['error']}"
        if not result.get("found"):
            return "not found in Cloudbric ThreatDB"

        bits = [f"threat level: {result['threat_level']}"]
        if result.get("crypto_currency"):
            bits.append(result["crypto_currency"])
        if result.get("reported_frequency"):
            bits.append(f"reported {result['reported_frequency']}x")
        if result.get("last_activity"):
            bits.append(f"last activity {result['last_activity']}")
        return "; ".join(bits)
