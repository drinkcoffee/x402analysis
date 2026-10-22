#!/usr/bin/env python3
"""Extracts the addresses marked as phishing from dataset/metadata_base.json
(09_fetch_metadata_base.py) into dataset/metadata_base_phishing_only.json.

An address is marked as phishing if its Blockscout name or tags include
"phish", ignoring case -- the same test 35_malicious_analysis.py and
36_server_usdc_received.py use (gatherlib/phishing.py). The new file keeps
metadata_base.json's details of where and when the metadata was gathered,
adds what was matched and how many addresses matched, and lists each
matching entry unchanged, sorted by address.

35_malicious_analysis.py and 36_server_usdc_received.py don't read this
file but a hand-reviewed copy of it,
dataset/metadata_base_phishing_only_annotated.json: copy it there, then
delete the entries that aren't really phishing, or edit their name and
tags. Re-running this doesn't touch that copy.

Doesn't use the database. Can be run any number of times; re-run after 09.

Usage:
    python gather/09a_extract_phishing.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

GATHER_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(GATHER_ROOT))

from gatherlib import DATASET_DIR  # noqa: E402
from gatherlib.common import write_json  # noqa: E402
from gatherlib.phishing import FIELDS, METADATA_PATH, WORD, phishing_entries  # noqa: E402

OUTPUT_PATH = DATASET_DIR / "metadata_base_phishing_only.json"
KEPT = ("gathered_at", "chain", "chain_id", "source")


def main() -> None:
    if not METADATA_PATH.exists():
        print(f"{METADATA_PATH.relative_to(GATHER_ROOT)} doesn't exist -- run 09_fetch_metadata_base.py first.", file=sys.stderr)
        sys.exit(2)
    metadata = json.loads(METADATA_PATH.read_text())
    entries = phishing_entries(metadata["addresses"])
    write_json(OUTPUT_PATH, {
        **{key: metadata[key] for key in KEPT if key in metadata},
        "extracted_from": METADATA_PATH.name,
        "match": f'name or tags include "{WORD}", ignoring case',
        "fields": list(FIELDS),
        "with_metadata": len(metadata["addresses"]),
        "phishing": len(entries),
        "addresses": entries,
    })
    print(
        f"{len(entries):,} of {len(metadata['addresses']):,} address(es) with metadata marked as phishing; "
        f"wrote {OUTPUT_PATH.relative_to(GATHER_ROOT)}"
    )


if __name__ == "__main__":
    main()
