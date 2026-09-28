"""Server-side HTTP client for x402-analysis-api's facilitator endpoints.

The dashboard's Facilitators tab and the facilitator detail page both need
this data, but the browser never calls x402-analysis-api directly: that API
requires an `X-API-Key` header, and a key embedded in client-side
JavaScript would be visible to anyone who views the page source or opens
the network tab -- there's no way to keep a secret in code the browser
downloads. Instead, /api/facilitators and /api/facilitators/{name} (see
api/app.py) proxy through this module server-side, where the key lives
only in an env var and is never sent to the browser.

Env vars:
    X402_API_BASE_URL   required -- e.g. https://x402-analysis-api.vercel.app
                        (no trailing slash)
    X402_API_KEY         required -- a "read" (or "read_write") API key for
                         that server; see its scripts/create_api_key.py
"""

from __future__ import annotations

import os
from typing import Optional
from urllib.parse import quote

import requests

DEFAULT_TIMEOUT = 10.0


def _base_url() -> str:
    url = os.getenv("X402_API_BASE_URL")
    if not url:
        raise RuntimeError("X402_API_BASE_URL is not set.")
    return url.rstrip("/")


def _headers() -> dict:
    key = os.getenv("X402_API_KEY")
    if not key:
        raise RuntimeError("X402_API_KEY is not set.")
    return {"X-API-Key": key}


def list_facilitators(timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """{"name", "risk", "active"} for every facilitator -- GET /facilitators
    on x402-analysis-api."""
    resp = requests.get(f"{_base_url()}/facilitators", headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def get_facilitator(name: str, timeout: float = DEFAULT_TIMEOUT) -> Optional[dict]:
    """Everything x402-analysis-api knows about one facilitator (GET
    /facilitators/{name}), or None if it has none by that name."""
    resp = requests.get(
        f"{_base_url()}/facilitators/{quote(name, safe='')}",
        headers=_headers(),
        timeout=timeout,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()
