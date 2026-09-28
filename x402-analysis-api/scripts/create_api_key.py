#!/usr/bin/env python3
"""Mints, lists, or revokes API keys for x402-analysis-api.

The running server has no self-service "create a key" endpoint -- this is
the only way to hand out access. A newly minted key is printed exactly
once: only its SHA-256 hash is stored, so if you lose it you must create a
new one.

Usage:
    python scripts/create_api_key.py --access read --label "some consumer"
    python scripts/create_api_key.py --access read_write --label "internal script"
    python scripts/create_api_key.py --list
    python scripts/create_api_key.py --revoke 3

Requires DATABASE_URL, read from a .env file in the project root (if
present) or the real environment.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "api"))  # apilib/ lives under api/, see api/app.py's docstring

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from apilib.auth import hash_api_key  # noqa: E402
from apilib.db import add_api_key, list_api_keys, revoke_api_key  # noqa: E402

# Purely a human-readable hint of a key's access level at a glance (e.g. in
# logs) -- the server never trusts this prefix, only the api_keys row its
# hash resolves to.
_PREFIXES = {"read": "x402ro", "read_write": "x402rw"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--access", choices=sorted(_PREFIXES), help="access level for a new key")
    parser.add_argument("--label", help="optional human-readable label for a new key")
    parser.add_argument(
        "--list", action="store_true", help="list existing keys (id/access level/label -- never the raw key) and exit"
    )
    parser.add_argument("--revoke", type=int, metavar="ID", help="revoke (delete) the key with this id")
    args = parser.parse_args()

    if args.list:
        rows = list_api_keys()
        if not rows:
            print("(no API keys yet)")
            return
        for row in rows:
            print(f"{row['id']}\t{row['access_level']}\t{row['label'] or ''}\t{row['created_at']}")
        return

    if args.revoke is not None:
        revoked = revoke_api_key(args.revoke)
        print(f"key {args.revoke} -> {'revoked' if revoked else 'was not present'}")
        return

    if not args.access:
        parser.error("--access is required unless --list or --revoke is given")

    raw_key = f"{_PREFIXES[args.access]}_{secrets.token_urlsafe(32)}"
    add_api_key(hash_api_key(raw_key), args.access, args.label)
    print("New API key (shown once -- store it now, only its hash is kept):")
    print(raw_key)


if __name__ == "__main__":
    main()
