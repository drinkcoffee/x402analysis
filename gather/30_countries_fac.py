#!/usr/bin/env python3
"""Counts how many facilitators' API servers and documentation servers are
in each country, from dataset/facilitators.json (written by
01_facilitators.py), and writes the counts as a LaTeX table to
dataset/facilitators-countries.tex.

A server's country is where its IP address geolocates: the last part of
the "City, Region, Country" location 01 recorded for the facilitator's
API URL or doc URL. A facilitator with no URL of a kind, or a URL with no
location, is counted as Unknown. Each column counts every facilitator
once, so a facilitator whose API and docs are on the same server is
counted in both.

The table has a row per country -- API servers, then documentation
servers -- sorted by API servers, most first (ties by documentation
servers, then name), with Unknown and a Total row last. A note under it
says that servers behind a CDN such as Cloudflare geolocate to the CDN's
location rather than the origin's. It's a table environment, for
\\input{facilitators-countries} in a paper, and uses booktabs
(\\usepackage{booktabs}).

Doesn't use the database. Can be run any number of times; each run
overwrites dataset/facilitators-countries.tex.

Usage:
    python gather/30_countries_fac.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

GATHER_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(GATHER_ROOT))

from gatherlib import DATASET_DIR  # noqa: E402
from gatherlib.countries import UNKNOWN, country, latex_table, write_tex  # noqa: E402

INPUT_PATH = DATASET_DIR / "facilitators.json"
OUTPUT_PATH = DATASET_DIR / "facilitators-countries.tex"


def main() -> None:
    if not INPUT_PATH.exists():
        print(f"{INPUT_PATH.relative_to(GATHER_ROOT)} doesn't exist -- run 01_facilitators.py first.", file=sys.stderr)
        sys.exit(2)
    facilitators = json.loads(INPUT_PATH.read_text())["facilitators"]
    api = Counter(country(f, "api_url") for f in facilitators)
    docs = Counter(country(f, "doc_url") for f in facilitators)
    write_tex(
        OUTPUT_PATH,
        latex_table(
            [("API servers", api), ("Documentation servers", docs)],
            caption=f"Countries of the {len(facilitators)} facilitators' API and documentation servers",
            label="tab:facilitators-countries",
            source="gather/30_countries_fac.py from dataset/facilitators.json",
        ),
    )
    print(
        f"{len(facilitators)} facilitator(s): API servers in {len(set(api) - {UNKNOWN})} countries, "
        f"documentation servers in {len(set(docs) - {UNKNOWN})}; wrote {OUTPUT_PATH.relative_to(GATHER_ROOT)}"
    )


if __name__ == "__main__":
    main()
