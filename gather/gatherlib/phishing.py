"""Which addresses are marked as phishing: those whose Blockscout name or
tags, in dataset/metadata_base.json (09_fetch_metadata_base.py), include
"phish", ignoring case -- so "Phish / Hack" tags and "Fake_Phishing..."
names both count. Shared by 35_malicious_analysis.py and
36_server_usdc_received.py.
"""

from __future__ import annotations

import json
import sys

from . import DATASET_DIR
from .funders import load_funders

METADATA_PATH = DATASET_DIR / "metadata_base.json"
WORD = "phish"
FIELDS = ("name", "tags")


def is_phishing(entry: dict) -> bool:
    """Whether a metadata_base.json entry's name or tags include WORD,
    ignoring case."""
    return any(WORD in (entry.get(field) or "").lower() for field in FIELDS)


def load_metadata() -> list[dict]:
    """metadata_base.json's "addresses" list; exits if it isn't there."""
    if not METADATA_PATH.exists():
        print(f"dataset/{METADATA_PATH.name} doesn't exist -- run 09_fetch_metadata_base.py first.", file=sys.stderr)
        sys.exit(2)
    return json.loads(METADATA_PATH.read_text())["addresses"]


def phishing_entries(addresses: list[dict] | None = None) -> list[dict]:
    """The metadata entries marked as phishing, sorted by address."""
    entries = load_metadata() if addresses is None else addresses
    return sorted((entry for entry in entries if is_phishing(entry)), key=lambda entry: entry["address"])


def phishing_and_funded() -> tuple[set[str], set[str]]:
    """(the addresses marked as phishing, the addresses phishing funders
    funded -- from dataset/funders.json or its pieces), lower case."""
    phishing = {entry["address"].lower() for entry in phishing_entries()}
    funded = {
        item["address"].lower()
        for funder in load_funders()
        if funder["address"].lower() in phishing
        for item in funder.get("funded") or []
    }
    return phishing, funded
