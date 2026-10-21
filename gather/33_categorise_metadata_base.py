#!/usr/bin/env python3
"""Lists the unique names and tags in dataset/metadata_base.json (Blockscout
metadata for Base addresses), as a starting point for categorising them.

Prints two sections to standard out: "# Names", then "# Tags", each with
one value per line, sorted alphabetically (ignoring case), followed by a
tab and the number of addresses that have it. Tags are kept as Blockscout
gives them -- a string like "Router; DEX; OKX Labs" is one value, not
split -- with only spaces at the ends trimmed. Addresses with no name or
no tags aren't listed; how many there are is printed to standard error.

Doesn't use the database. Can be run any number of times.

Usage:
    python gather/33_categorise_metadata_base.py > names-and-tags.txt
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

GATHER_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(GATHER_ROOT))

from gatherlib import DATASET_DIR  # noqa: E402

INPUT_PATH = DATASET_DIR / "metadata_base.json"


def unique(addresses: list[dict], field: str) -> tuple[Counter, int]:
    """How many addresses have each value of `field` (trimmed), and how
    many have none."""
    values = [(entry.get(field) or "").strip() for entry in addresses]
    return Counter(value for value in values if value), values.count("")


def print_section(title: str, counts: Counter) -> None:
    print(f"# {title}")
    for value in sorted(counts, key=lambda v: (v.casefold(), v)):
        print(f"{value}\t{counts[value]:,}")


def main() -> None:
    if not INPUT_PATH.exists():
        print(f"{INPUT_PATH.relative_to(GATHER_ROOT)} doesn't exist.", file=sys.stderr)
        sys.exit(2)
    addresses = json.loads(INPUT_PATH.read_text())["addresses"]
    names, no_name = unique(addresses, "name")
    tags, no_tags = unique(addresses, "tags")
    print_section("Names", names)
    print()
    print_section("Tags", tags)
    print(
        f"{len(addresses):,} address(es): {len(names):,} unique name(s) ({no_name:,} with none), "
        f"{len(tags):,} unique tags string(s) ({no_tags:,} with none)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
