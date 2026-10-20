#!/usr/bin/env python3
"""Counts how many services are in each category, from
dataset/services-annotated.json (services.json, from 02_services.py, with a
"category" added to each service by hand), writes the counts as a LaTeX
table to dataset/service-categories.tex, and prints the same table.

Each service (endpoint) is counted once, under its category exactly as
annotated -- names aren't merged or corrected, so "*pii-redact" and
"pii-redact" are separate rows. A service with no category counts as
uncategorised. Categories are shown as readable labels (label()): hyphens
become spaces, the first letter is capitalised, and the words in WORDS
are spelt out, so "software-dev" is "Software development" and "llm" is
"LLM" (but "x402" stays lower case).

The table has a row per category: first the categories whose names start
with "*", then the rest, each group in alphabetical order of its label
(sort_key(): ignoring case and the "*"), with Uncategorised, Service
inactive and a Total row last. It's a
table environment, for \\input{service-categories} in a paper, and uses
booktabs (\\usepackage{booktabs}).

Doesn't use the database. Can be run any number of times; each run
overwrites dataset/service-categories.tex.

Usage:
    python gather/32_service_categories.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

GATHER_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(GATHER_ROOT))

from gatherlib import DATASET_DIR  # noqa: E402
from gatherlib.latex import latex_table, text_table, write_tex  # noqa: E402

INPUT_PATH = DATASET_DIR / "services-annotated.json"
OUTPUT_PATH = DATASET_DIR / "service-categories.tex"
UNCATEGORISED = "uncategorised"
INACTIVE = "service-inactive"
# How words in category names are shown; any other word is shown as is.
WORDS = {
    "dev": "development",
    "util": "utilities",
    "llm": "LLM",
    "pii": "PII",
    "jwt": "JWT",
    "vm": "VM",
    "vpn": "VPN",
    "ip": "IP",
    "hmac": "HMAC",
    "totp": "TOTP",
    "hotp": "HOTP",
    "pbkdf2": "PBKDF2",
    "base64": "Base64",
}
# Words that keep a lower-case first letter at the start of a label.
LOWER_CASE = {"x402"}


def label(category: str) -> str:
    """`category` as a readable label: "*software-dev-jwt-decode" ->
    "*Software development JWT decode"."""
    star = "*" if category.startswith("*") else ""
    words = [WORDS.get(word, word) for word in category.lstrip("*").split("-") if word]
    if words and words[0] not in LOWER_CASE:
        words[0] = words[0][:1].upper() + words[0][1:]
    return star + " ".join(words)


def sort_key(category: str) -> tuple[bool, str, str]:
    """Categories starting with "*" first, then the rest; each group
    alphabetical by label, ignoring case and the "*"."""
    text = label(category)
    return not text.startswith("*"), text.lstrip("*").casefold(), text


def main() -> None:
    if not INPUT_PATH.exists():
        print(f"{INPUT_PATH.relative_to(GATHER_ROOT)} doesn't exist.", file=sys.stderr)
        sys.exit(2)
    servers = json.loads(INPUT_PATH.read_text())["servers"]
    categories = Counter(
        (service.get("category") or "").strip() or UNCATEGORISED
        for server in servers
        for service in server.get("services") or []
    )
    columns = [("Services", categories)]
    last_rows = (UNCATEGORISED, INACTIVE)
    total = sum(categories.values())
    write_tex(
        OUTPUT_PATH,
        latex_table(
            "Category",
            columns,
            caption=f"Categories of the {total:,} services offered by {len(servers):,} servers",
            label="tab:service-categories",
            source="gather/32_service_categories.py from dataset/services-annotated.json",
            last_rows=last_rows,
            display=label,
            order=sort_key,
        ),
    )
    print(text_table("Category", columns, last_rows=last_rows, display=label, order=sort_key))
    print()
    print(
        f"{total:,} service(s) on {len(servers):,} server(s) in "
        f"{len(set(categories) - set(last_rows))} categories; wrote {OUTPUT_PATH.relative_to(GATHER_ROOT)}"
    )


if __name__ == "__main__":
    main()
