#!/usr/bin/env python3
"""Calls GET {api_url}/supported for every facilitator that has an api URL
recorded (facilitator.api, joined to uris.url), and reports any facilitator
whose response body isn't valid JSON -- the shape a well-behaved x402
facilitator's /supported endpoint is expected to return (see the sibling
cli-tool project's x402tool.generic_client.GenericFacilitatorClient.supported,
which hits the same path).

A facilitator with no api URL on file is skipped -- there's nothing to call.

For each facilitator whose /supported response is not valid JSON (including
one the request couldn't reach at all -- DNS failure, connection refused,
timeout, non-JSON error page, etc.), prints the facilitator's name, its api
URL, and the raw text that came back (or a description of the request
failure, if no response was received at all).

Usage:
    python scripts/check_supported.py

Requires DATABASE_URL, read from a .env file in the project root (if
present) or the real environment.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

import os  # noqa: E402

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
import requests  # noqa: E402

REQUEST_TIMEOUT = 10.0
TEXT_PREVIEW_LIMIT = 500


def fetch_facilitator_apis(cur) -> list[dict]:
    """{"name", "api_url"} for every facilitator with an api URL on file,
    ordered by name."""
    cur.execute(
        """
        SELECT f.name AS name, u.url AS api_url
        FROM facilitator f
        JOIN uris u ON u.id = f.api
        WHERE f.api IS NOT NULL
        ORDER BY f.name
        """
    )
    return [dict(row) for row in cur.fetchall()]


def check_supported(api_url: str) -> tuple[bool, str]:
    """(is_valid_json, text) -- text is the raw response body if one was
    received, or a description of the request failure otherwise."""
    url = api_url.rstrip("/") + "/supported"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        return False, f"(request failed: {exc})"

    try:
        response.json()
    except ValueError:
        return False, response.text

    return True, response.text


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            facilitators = fetch_facilitator_apis(cur)
    finally:
        conn.close()

    print(f"checking /supported for {len(facilitators)} facilitator(s) with an api URL...")

    failures = []
    for facilitator in facilitators:
        name = facilitator["name"]
        api_url = facilitator["api_url"]
        is_valid_json, text = check_supported(api_url)
        if not is_valid_json:
            failures.append((name, api_url, text))

    if not failures:
        print("All facilitators returned valid JSON from /supported.")
        return

    print(f"\n{len(failures)} facilitator(s) did not return valid JSON from /supported:\n")
    for name, api_url, text in failures:
        preview = text if len(text) <= TEXT_PREVIEW_LIMIT else text[:TEXT_PREVIEW_LIMIT] + "... (truncated)"
        print(f"{name}\t{api_url}/supported")
        print(f"  {preview}\n")


if __name__ == "__main__":
    main()
