#!/usr/bin/env python3
"""Counts how many servers' API servers and documentation servers are in
each country, from dataset/services.json (written by 02_services.py), and
writes the counts as a LaTeX table to dataset/servers-countries.tex -- as
30_countries_fac.py does for facilitators.

A server's country is where its IP address geolocates: the last part of
the "City, Region, Country" location 02 recorded for the server's API URL
or doc URL. Every server has an API URL, but most have no doc URL; those
are counted in a "No documentation URL" row, so both columns total the
number of servers. A URL with no location (one that didn't resolve) is
counted as Unknown. A server whose API and docs are on the same host is
counted in both columns.

The table has a row per country -- API servers, then documentation
servers -- sorted by API servers, most first (ties by documentation
servers, then name), with "No documentation URL", Unknown and a Total
row last. A note under it says that servers behind a CDN such as
Cloudflare geolocate to the CDN's location rather than the origin's. It's
a table environment, for \\input{servers-countries} in a paper, and uses
booktabs (\\usepackage{booktabs}).

Doesn't use the database. Can be run any number of times; each run
overwrites dataset/servers-countries.tex.

Usage:
    python gather/31_countries_servers.py
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

INPUT_PATH = DATASET_DIR / "services.json"
OUTPUT_PATH = DATASET_DIR / "servers-countries.tex"
NO_DOC_URL = "No documentation URL"


def main() -> None:
    if not INPUT_PATH.exists():
        print(f"{INPUT_PATH.relative_to(GATHER_ROOT)} doesn't exist -- run 02_services.py first.", file=sys.stderr)
        sys.exit(2)
    servers = json.loads(INPUT_PATH.read_text())["servers"]
    api = Counter(country(s, "api_url") for s in servers)
    docs = Counter(country(s, "doc_url", no_url=NO_DOC_URL) for s in servers)
    write_tex(
        OUTPUT_PATH,
        latex_table(
            [("API servers", api), ("Documentation servers", docs)],
            caption=f"Countries of the {len(servers):,} servers' API and documentation servers",
            label="tab:servers-countries",
            source="gather/31_countries_servers.py from dataset/services.json",
            last_rows=(NO_DOC_URL, UNKNOWN),
        ),
    )
    print(
        f"{len(servers):,} server(s): API servers in {len(set(api) - {UNKNOWN})} countries, "
        f"documentation servers in {len(set(docs) - {UNKNOWN, NO_DOC_URL})} ({docs[NO_DOC_URL]:,} servers have "
        f"no documentation URL); wrote {OUTPUT_PATH.relative_to(GATHER_ROOT)}"
    )


if __name__ == "__main__":
    main()
