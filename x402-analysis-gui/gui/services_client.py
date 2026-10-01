"""Server-side HTTP client for x402-analysis-api's service endpoints.

Same reasoning as gui/facilitators_client.py: the browser never calls
x402-analysis-api directly (that would mean embedding X402_API_KEY in
client-side JavaScript, visible to anyone who views the page source), so
/api/services and /api/services/{id} (see api/app.py) proxy through this
module server-side, where the key lives only in an env var.

Env vars:
    X402_API_BASE_URL   required -- e.g. https://x402-analysis-api.vercel.app
                        (no trailing slash)
    X402_API_KEY         required -- a "read" (or "read_write") API key for
                         that server; see its scripts/create_api_key.py
"""

from __future__ import annotations

import os
from typing import Optional, Sequence

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


def list_services(
    limit: int,
    offset: int,
    categories: Optional[Sequence[str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """{"items", "total", "limit", "offset"} -- GET /services on
    x402-analysis-api, paged (there can be thousands of services) and
    optionally filtered to one or more categories."""
    params = {"limit": limit, "offset": offset}
    if categories:
        params["category"] = list(categories)
    resp = requests.get(
        f"{_base_url()}/services",
        params=params,
        headers=_headers(),
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def list_service_categories(timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """Every distinct category currently assigned to at least one service,
    sorted -- GET /services/categories on x402-analysis-api."""
    resp = requests.get(
        f"{_base_url()}/services/categories",
        headers=_headers(),
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def get_service(service_id: int, timeout: float = DEFAULT_TIMEOUT) -> Optional[dict]:
    """Everything x402-analysis-api knows about one service (GET
    /services/{id}), or None if no service has that id."""
    resp = requests.get(
        f"{_base_url()}/services/{service_id}",
        headers=_headers(),
        timeout=timeout,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()
