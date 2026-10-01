"""Server-side HTTP client for x402-analysis-api's server endpoints.

Same reasoning as gui/facilitators_client.py: the browser never calls
x402-analysis-api directly (that would mean embedding X402_API_KEY in
client-side JavaScript, visible to anyone who views the page source), so
/api/servers and /api/servers/{name} (see api/app.py) proxy through this
module server-side, where the key lives only in an env var.

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


def list_servers(limit: int, offset: int, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """{"items", "total", "limit", "offset"} -- GET /servers on
    x402-analysis-api, paged (there can be thousands of servers)."""
    resp = requests.get(
        f"{_base_url()}/servers",
        params={"limit": limit, "offset": offset},
        headers=_headers(),
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def get_server(name: str, timeout: float = DEFAULT_TIMEOUT) -> Optional[dict]:
    """Everything x402-analysis-api knows about one server (GET
    /servers/{name}), including its services, or None if it has none by
    that name."""
    resp = requests.get(
        f"{_base_url()}/servers/{quote(name, safe='')}",
        headers=_headers(),
        timeout=timeout,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()
