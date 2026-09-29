#!/usr/bin/env python3
"""Calls the /supported endpoint for every facilitator that has an api URL
recorded (facilitator.api, joined to uris.url), and reports any facilitator
whose response body isn't valid JSON -- the shape a well-behaved x402
facilitator's /supported endpoint is expected to return.

Most facilitators implement the plain, unauthenticated x402 contract --
GET {api_url}/supported (see the sibling cli-tool project's
x402tool.generic_client.GenericFacilitatorClient.supported, which hits the
same path) -- and are called that way here.

Coinbase's CDP Platform API (api.cdp.coinbase.com) is a special case: it's
an authenticated API whose /supported route is versioned at
/v2/x402/supported rather than sitting at the bare base URL, and it rejects
unsigned requests outright ("not authorised"), rather than returning a
non-JSON error body a plain GET would surface. A facilitator whose api URL
is on that host is instead called via CdpClient (cdp_client.py, in this
same directory), which signs a short-lived bearer JWT per request the way
CDP's API requires (see cdp_auth.py). cdp_client.py/cdp_auth.py are
vendored, byte-for-byte, from the sibling cli-tool project's
x402tool/cdp_client.py and x402tool/cdp_auth.py -- so this script doesn't
need cli-tool checked out next to this project just to authenticate to
Coinbase; pyjwt/cryptography (their only extra dependencies) are in this
project's own requirements.txt for the same reason.

That needs a CDP API key pair: CDP_API_KEY_ID / CDP_API_KEY_SECRET, free
from https://portal.cdp.coinbase.com/ -- read from a .env file in *either*
this project's root or cli-tool's (so an existing cli-tool/.env, if you
already use the CLI against Coinbase, doesn't need to be duplicated here).
Checked by preflight_checks(), which main() runs up front (alongside a
DATABASE_URL/connectivity check) before anything else, so a missing CDP
setup is reported clearly and the whole run stops immediately, rather than
only turning up as Coinbase's entry in the failures list partway through.
Required libraries (psycopg2, requests, pyjwt, cryptography) are checked
even earlier than that, via a guarded import at the top of this file --
missing any of them prints which one and exits immediately, since nothing
here can run at all without them.

A facilitator with no api URL on file is skipped -- there's nothing to call.

For each facilitator whose /supported response is not valid JSON (including
one the request couldn't reach at all -- DNS failure, connection refused,
timeout, non-JSON error page, etc.), prints the facilitator's name, the URL
actually called, and the raw text that came back (or a description of the
request failure, if no response was received at all).

Usage:
    python scripts/check_supported.py

Requires DATABASE_URL and CDP_API_KEY_ID/CDP_API_KEY_SECRET, read from a
.env file in the project root (if present) or the real environment.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI_TOOL_ROOT = PROJECT_ROOT.parent / "cli-tool"

import preflight  # noqa: E402

try:
    from dotenv import load_dotenv
    import psycopg2
    import psycopg2.extras
    import requests
    # cdp_client/cdp_auth pull in pyjwt and cryptography -- caught here too,
    # under the same "pip install -r requirements.txt" message, rather than
    # a separate one just for these two.
    from cdp_client import CDP_BASE_URL, CDP_HOST, CdpClient, CdpClientError
except ImportError as exc:
    print(
        f"missing required package: {exc.name}. Run `pip install -r "
        "requirements.txt` from x402-analysis-api/, with your virtualenv "
        "active.",
        file=sys.stderr,
    )
    sys.exit(1)

load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(CLI_TOOL_ROOT / ".env")  # e.g. CDP_API_KEY_ID/SECRET, if set up for the CLI already

import os  # noqa: E402

REQUEST_TIMEOUT = 10.0
TEXT_PREVIEW_LIMIT = 500

CDP_SUPPORTED_PATH = "/v2/x402/supported"


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


def _parse_json(response: requests.Response) -> tuple[bool, str]:
    try:
        response.json()
    except ValueError:
        return False, response.text
    return True, response.text


def _check_cdp_supported() -> tuple[str, bool, str]:
    """Coinbase's CDP Platform API: authenticated, versioned path. Returns
    (url_called, is_valid_json, text), same as _check_generic_supported."""
    url = CDP_BASE_URL.rstrip("/") + CDP_SUPPORTED_PATH

    # CDP_API_KEY_ID/CDP_API_KEY_SECRET are guaranteed set by this point --
    # _require_cdp_env_vars() already checked at startup, before main() got
    # this far.
    client = CdpClient(
        key_id=os.environ["CDP_API_KEY_ID"],
        key_secret=os.environ["CDP_API_KEY_SECRET"],
        timeout=REQUEST_TIMEOUT,
    )
    try:
        response = client.supported()
    except (CdpClientError, requests.RequestException) as exc:
        return url, False, f"(request failed: {exc})"
    is_valid_json, text = _parse_json(response)
    return response.url, is_valid_json, text


def _check_generic_supported(api_url: str) -> tuple[str, bool, str]:
    """The plain x402 facilitator contract: GET {api_url}/supported, no
    auth. Returns (url_called, is_valid_json, text)."""
    url = api_url.rstrip("/") + "/supported"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        return url, False, f"(request failed: {exc})"
    is_valid_json, text = _parse_json(response)
    return response.url, is_valid_json, text


def check_supported(api_url: str) -> tuple[str, bool, str]:
    """(url_called, is_valid_json, text) -- text is the raw response body
    if one was received, or a description of the request failure
    otherwise. Routed to the CDP-authenticated check if api_url is on
    Coinbase's CDP host, the plain unauthenticated check otherwise."""
    if urlparse(api_url).hostname == CDP_HOST:
        return _check_cdp_supported()
    return _check_generic_supported(api_url)


def preflight_checks() -> list[str]:
    """Problems that would stop check_supported() from being able to check
    Coinbase specifically: CDP_API_KEY_ID/CDP_API_KEY_SECRET. Required
    libraries (psycopg2/requests/pyjwt/cryptography) are already guaranteed
    importable by this point -- this module's own top-level import already
    exited with a clear message if any were missing -- so they're not
    re-checked here. This is what update.py additionally aggregates when it
    reuses check_supported() directly, without going through this script's
    own main()."""
    missing = [name for name in ("CDP_API_KEY_ID", "CDP_API_KEY_SECRET") if not os.getenv(name)]
    if not missing:
        return []
    return [
        "Missing required env var(s): "
        + ", ".join(missing)
        + ". This script authenticates to Coinbase's CDP Platform API with "
        "a CDP API key pair. To set one up: (1) go to "
        "https://portal.cdp.coinbase.com/, (2) go to Settings, API Keys, "
        "Secret API keys, (3) set CDP_API_KEY_ID and CDP_API_KEY_SECRET -- "
        "either export them, or add them to a .env file in this project's "
        "root (or cli-tool's, if you already use the CLI against Coinbase)."
    ]


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    preflight.check_or_exit(
        preflight_checks,
        preflight.require_database_url(database_url),
    )

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
        url_called, is_valid_json, text = check_supported(facilitator["api_url"])
        if not is_valid_json:
            failures.append((name, url_called, text))

    if not failures:
        print("All facilitators returned valid JSON from /supported.")
        return

    print(f"\n{len(failures)} facilitator(s) did not return valid JSON from /supported:\n")
    for name, url_called, text in failures:
        preview = text if len(text) <= TEXT_PREVIEW_LIMIT else text[:TEXT_PREVIEW_LIMIT] + "... (truncated)"
        print(f"{name}\t{url_called}")
        print(f"  {preview}\n")


if __name__ == "__main__":
    main()
