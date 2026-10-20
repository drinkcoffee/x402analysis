"""Counting servers by the country their IP address geolocates to, and
writing the counts as a LaTeX table (see latex.py) -- shared by
30_countries_fac.py and 31_countries_servers.py.

A URL's country is the last part of the "City, Region, Country" location
its fingerprint has (see common.fingerprint_urls), with the few names
ip-api.com spells two ways made one (COUNTRY_NAMES).
"""

from __future__ import annotations

from collections import Counter

from . import latex
from .latex import write_tex  # noqa: F401 -- for the 3x country scripts

UNKNOWN = "Unknown"
COUNTRY_NAMES = {"The Netherlands": "Netherlands"}
CDN_NOTE = (
    "Countries are where each server's IP address geolocates. Servers behind a CDN, such as\n"
    "  Cloudflare, geolocate to the CDN's location rather than the origin server's."
)


def country(entry: dict, url_field: str, no_url: str = UNKNOWN) -> str:
    """The country `entry`'s (a facilitator's or server's) URL in
    `url_field` geolocates to: `no_url` if it has no such URL, UNKNOWN if
    the URL has no location."""
    url = entry.get(url_field)
    if not url:
        return no_url
    info = (entry.get("fingerprints") or {}).get(url) or {}
    name = (info.get("location") or "").rsplit(",", 1)[-1].strip()
    return COUNTRY_NAMES.get(name, name) or UNKNOWN


def latex_table(
    columns: list[tuple[str, Counter]],
    caption: str,
    label: str,
    source: str,
    last_rows: tuple[str, ...] = (UNKNOWN,),
) -> str:
    """A booktabs table environment (see latex.latex_table): a row per
    country, then a column per (title, country -> count) in `columns`,
    with `last_rows` held back to the bottom and CDN_NOTE under it."""
    return latex.latex_table("Country", columns, caption, label, source, last_rows=last_rows, note=CDN_NOTE)
