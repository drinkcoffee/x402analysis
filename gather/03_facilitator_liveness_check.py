#!/usr/bin/env python3
"""Checks which facilitators are live and records it: sets every
facilitator's `active` in gather's database, and writes the results to
dataset/facilitator_liveness.json. The same check as step 4 of
x402-analysis-api/scripts/update.py (its /supported check is copied from
x402-analysis-api/scripts/check_supported.py).

A facilitator is active if its API's /supported endpoint returns valid
JSON -- what a working x402 facilitator returns. One with no API URL, or
whose /supported can't be reached or doesn't return JSON, is not active.

  - most facilitators: an unauthenticated GET {api url}/supported.
  - Coinbase (an API URL on api.cdp.coinbase.com): an authenticated GET
    of /v2/x402/supported, which needs a CDP API key pair --
    CDP_API_KEY_ID / CDP_API_KEY_SECRET in gather/.env (free from
    https://portal.cdp.coinbase.com/, under Settings, API Keys, Secret API
    keys). Only needed if a facilitator's API is on that host.

Can be run any number of times, e.g. after 01_facilitators.py and then
again later: each run updates every facilitator and overwrites
dataset/facilitator_liveness.json.

Usage:
    python gather/03_facilitator_liveness_check.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

GATHER_ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    import psycopg2  # noqa: F401
    import requests
except ImportError as exc:
    print(
        f"missing required package: {exc.name}. Run `pip install -r gather/requirements.txt`, "
        "with gather's virtualenv active.",
        file=sys.stderr,
    )
    sys.exit(1)

load_dotenv(GATHER_ROOT / ".env")

from gatherlib import DATASET_DIR, localdb, preflight  # noqa: E402
from gatherlib.cdp_client import CDP_BASE_URL, CDP_HOST, CdpClient, CdpClientError  # noqa: E402
from gatherlib.common import write_json  # noqa: E402

OUTPUT_PATH = DATASET_DIR / "facilitator_liveness.json"
REQUEST_TIMEOUT = 10.0
RESPONSE_PREVIEW_LIMIT = 500
CDP_SUPPORTED_PATH = "/v2/x402/supported"


def fetch_facilitators(cur) -> list[dict]:
    cur.execute(
        """
        SELECT f.name, u.url AS api_url
        FROM facilitator f
        LEFT JOIN uris u ON u.id = f.api
        ORDER BY lower(f.name)
        """
    )
    return [{"name": name, "api_url": api_url} for name, api_url in cur.fetchall()]


def _parse_json(response: requests.Response) -> tuple[bool, str]:
    try:
        response.json()
    except ValueError:
        return False, response.text
    return True, response.text


def _check_cdp_supported() -> tuple[str, int | None, bool, str]:
    """Coinbase's CDP Platform API: authenticated, versioned path."""
    url = CDP_BASE_URL.rstrip("/") + CDP_SUPPORTED_PATH
    client = CdpClient(
        key_id=os.environ["CDP_API_KEY_ID"],
        key_secret=os.environ["CDP_API_KEY_SECRET"],
        timeout=REQUEST_TIMEOUT,
    )
    try:
        response = client.supported()
    except (CdpClientError, requests.RequestException) as exc:
        return url, None, False, f"(request failed: {exc})"
    is_valid_json, text = _parse_json(response)
    return response.url, response.status_code, is_valid_json, text


def _check_generic_supported(api_url: str) -> tuple[str, int | None, bool, str]:
    """The plain x402 facilitator contract: GET {api_url}/supported."""
    url = api_url.rstrip("/") + "/supported"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        return url, None, False, f"(request failed: {exc})"
    is_valid_json, text = _parse_json(response)
    return response.url, response.status_code, is_valid_json, text


def check_supported(api_url: str) -> tuple[str, int | None, bool, str]:
    """(url called, HTTP status or None, is valid JSON, response text or
    a description of why the request failed)."""
    if urlparse(api_url).hostname == CDP_HOST:
        return _check_cdp_supported()
    return _check_generic_supported(api_url)


def require_cdp_keys(facilitators: list[dict]):
    """A preflight check: CDP keys are set, if any facilitator needs them."""

    def check() -> str | None:
        if not any(urlparse(f["api_url"] or "").hostname == CDP_HOST for f in facilitators):
            return None
        missing = [name for name in ("CDP_API_KEY_ID", "CDP_API_KEY_SECRET") if not os.getenv(name)]
        if not missing:
            return None
        return (
            f"missing {', '.join(missing)}, needed to check Coinbase's facilitator. Get a CDP API "
            "key pair at https://portal.cdp.coinbase.com/ (Settings, API Keys, Secret API keys) and "
            "add CDP_API_KEY_ID and CDP_API_KEY_SECRET to gather/.env."
        )

    return check


def main() -> None:
    preflight.check_or_exit(localdb.preflight_checks)
    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur:
            facilitators = fetch_facilitators(cur)
    finally:
        conn.close()
    preflight.check_or_exit(require_cdp_keys(facilitators))

    if not facilitators:
        print("no facilitators in the database -- run 01_facilitators.py first.", file=sys.stderr)
        sys.exit(2)

    total = len(facilitators)
    print(f"checking {total} facilitator(s)")
    results = []
    for n, f in enumerate(facilitators, 1):
        print(f"[{n}/{total}] {f['name']}: ", end="", flush=True)
        if not f["api_url"]:
            print("no API URL -- not active")
            results.append({**f, "url_called": None, "status": None, "active": False, "response": None})
            continue
        url_called, status, active, text = check_supported(f["api_url"])
        preview = text if len(text) <= RESPONSE_PREVIEW_LIMIT else text[:RESPONSE_PREVIEW_LIMIT] + "... (truncated)"
        why = "valid JSON" if active else (f"HTTP {status}, not JSON" if status is not None else text.strip("()"))
        print(f"{'active' if active else 'not active'} ({why})")
        results.append(
            {**f, "url_called": url_called, "status": status, "active": active, "response": preview}
        )

    # The network checks above can take a while, so the connection is only
    # opened again now.
    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur:
            for r in results:
                cur.execute(
                    "UPDATE facilitator SET active = %s, updated = CURRENT_DATE WHERE name = %s",
                    (r["active"], r["name"]),
                )
        conn.commit()
    finally:
        conn.close()

    write_json(
        OUTPUT_PATH,
        {
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "facilitators": results,
        },
    )
    active = sum(1 for r in results if r["active"])
    print(f"{active} of {total} facilitator(s) active; wrote {OUTPUT_PATH.relative_to(GATHER_ROOT)}")


if __name__ == "__main__":
    main()
