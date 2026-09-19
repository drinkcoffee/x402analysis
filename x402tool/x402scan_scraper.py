"""Scraper for x402scan.com's server/service directory.

x402scan.com has no documented public API for its server/service listing.
It's a Next.js (App Router) site, and the data driving each page is
embedded directly in the page's HTML as React Server Component payload -
a series of `self.__next_f.push([1, "<ref>:<json...>"])` script calls whose
string argument, once JSON-decoded, is `"<ref-id>:" + <json text>`. Rather
than driving a browser, this module fetches the plain HTML and pulls the
relevant JSON straight out of those chunks. (Reverse-engineered by watching
the site's own network traffic; the exact shape isn't a stable public
contract, so this may need updating if x402scan changes its data model.)

Two page shapes matter here:

  GET https://www.x402scan.com/
    embeds a React Query cache entry shaped like:
      {"items": [
        {
          "recipients": ["0x...", ...],   # a sample of payTo addresses
          "origins": [
            {"id": "<uuid>", "origin": "https://...", "title": "...",
             "description": "...", "favicon": "..."},
            ...
          ],
          "facilitators": [...], "tx_count": ..., "chains": [...], ...
        },
        ...
      ]}
    One "item" is a *server group*: one or more origins (domains) that
    x402scan groups together (by shared branding/ownership - e.g. a
    server's API subdomain plus its marketing homepage).

  GET https://www.x402scan.com/server/<originId>
    embeds one origin's full detail:
      {"id", "origin", "title", "description", "favicon",
       "resources": [
         {"id", "resource" (endpoint URL), "method", "type", "x402Version",
          "metadata": {"price", "description", "pricingMode"},
          "accepts": [{"payTo", "network", "asset", "maxAmountRequired", ...}],
          "tags": [{"tag": {"name": ...}}]},
         ...
       ]}
    A given origin's full, precise address set is the unique set of
    `accepts[].payTo` values across its resources - more complete than the
    listing page's `recipients` sample.

There's no separate "documentation URL" field anywhere in this data; when a
server group has more than one origin, the extra origin (typically the bare
marketing domain alongside an api./x402. subdomain) is reported as the doc
link, on the heuristic in `_pick_primary_origin`.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional
from urllib.parse import urlparse

import requests

BASE_URL = "https://www.x402scan.com"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
_PUSH_MARKER = "self.__next_f.push([1,"


def _fetch_html(url: str, timeout: float) -> str:
    r = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    return r.text


def _decode_push_chunks(html: str) -> list[str]:
    """Decode every self.__next_f.push([1,"..."]) call's string argument
    back to raw text (undoing the JS string-literal escaping)."""
    chunks: list[str] = []
    i = 0
    while True:
        i = html.find(_PUSH_MARKER, i)
        if i == -1:
            break
        start = html.find('"', i + len(_PUSH_MARKER))
        if start == -1:
            break
        j = start + 1
        while True:
            j = html.find('"', j)
            if j == -1:
                break
            backslashes = 0
            k = j - 1
            while k >= 0 and html[k] == "\\":
                backslashes += 1
                k -= 1
            if backslashes % 2 == 0:
                break
            j += 1
        if j == -1:
            break
        try:
            chunks.append(json.loads(html[start : j + 1]))
        except json.JSONDecodeError:
            pass
        i = j + 1
    return chunks


def _json_islands(text: str, marker: str = '"json":') -> list[Any]:
    """Every `"json": <value>` value embedded in a decoded RSC chunk, parsed
    with raw_decode so surrounding non-JSON React-element syntax (bare
    `$undefined`, `"$L4f"` component refs, etc.) doesn't break parsing.

    Different cached queries wrap their dehydrated data differently - some
    as `"json":{...}` (an object), others as `"json":[...]` (an array, e.g.
    a query returning a bare list) - so both are tried.
    """
    decoder = json.JSONDecoder()
    islands: list[Any] = []
    i = 0
    while True:
        i = text.find(marker, i)
        if i == -1:
            break
        value_start = i + len(marker)
        if value_start >= len(text) or text[value_start] not in "{[":
            i = value_start
            continue
        try:
            obj, _end = decoder.raw_decode(text, value_start)
            islands.append(obj)
        except json.JSONDecodeError:
            pass
        i = value_start + 1
    return islands


def _candidate_dicts(island: Any) -> list[dict[str, Any]]:
    """A dehydrated query's "json" value is sometimes the object we want
    directly, sometimes a list wrapping it (e.g. `"json":[{...}]`)."""
    if isinstance(island, dict):
        return [island]
    if isinstance(island, list):
        return [item for item in island if isinstance(item, dict)]
    return []


def fetch_server_groups(timeout: float = 20.0) -> list[dict[str, Any]]:
    """The full list of server groups embedded in the homepage."""
    html = _fetch_html(f"{BASE_URL}/", timeout=timeout)
    for chunk in _decode_push_chunks(html):
        for island in _json_islands(chunk):
            for candidate in _candidate_dicts(island):
                items = candidate.get("items")
                if isinstance(items, list) and items and isinstance(items[0], dict) and "origins" in items[0]:
                    return items
    return []


def fetch_origin_detail(origin_id: str, timeout: float = 20.0) -> Optional[dict[str, Any]]:
    """One origin's own detail page: its resources and metadata.

    The page embeds more than one cached query with a top-level "resources"
    list - a minimal tags/accepts-only projection (used for a count) as
    well as the full one carrying each resource's URL/method/metadata. Only
    the latter is useful here, identified by its entries actually having a
    "resource" (URL) field.
    """
    html = _fetch_html(f"{BASE_URL}/server/{origin_id}", timeout=timeout)
    candidates: list[dict[str, Any]] = []
    for chunk in _decode_push_chunks(html):
        for island in _json_islands(chunk):
            for candidate in _candidate_dicts(island):
                if isinstance(candidate.get("resources"), list):
                    candidates.append(candidate)
    for candidate in candidates:
        resources = candidate["resources"]
        if resources and isinstance(resources[0], dict) and "resource" in resources[0]:
            return candidate
    return candidates[0] if candidates else None


def _pick_primary_origin(origins: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(primary "API" origin, remaining origins) for a server group.

    With one origin there's nothing to pick. With more than one, prefer the
    origin that looks like an API host (contains "api"/"x402", or is a
    subdomain) over a bare marketing domain, which is reported separately
    as the doc/homepage link.
    """
    if len(origins) <= 1:
        return origins[0], []

    def score(o: dict[str, Any]) -> int:
        host = (urlparse(o.get("origin", "")).hostname or "").lower()
        s = 0
        if "api" in host:
            s += 2
        if host.startswith("x402"):
            s += 2
        if host.count(".") >= 2:
            s += 1
        return s

    ordered = sorted(origins, key=score, reverse=True)
    return ordered[0], ordered[1:]


def _resource_summary(resource: dict[str, Any]) -> dict[str, Any]:
    metadata = resource.get("metadata") or {}
    tags = [
        t["tag"]["name"]
        for t in (resource.get("tags") or [])
        if isinstance(t, dict) and isinstance(t.get("tag"), dict) and t["tag"].get("name")
    ]
    return {
        "url": resource.get("resource"),
        "method": resource.get("method"),
        "description": metadata.get("description") or resource.get("description"),
        "price": metadata.get("price"),
        "tags": tags,
    }


def scrape_server(group: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
    """Full scrape for one server group: visit every origin's detail page
    and combine their resources/addresses."""
    origins = group.get("origins") or []
    primary, others = _pick_primary_origin(origins)

    resources: list[dict[str, Any]] = []
    addresses: set[str] = set(group.get("recipients") or [])
    name = primary.get("title") or urlparse(primary.get("origin", "")).hostname
    description = primary.get("description")
    errors: list[str] = []

    for origin in origins:
        origin_id = origin.get("id")
        if not origin_id:
            continue
        try:
            detail = fetch_origin_detail(origin_id, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            errors.append(f"{origin.get('origin')}: {exc}")
            continue
        if not detail:
            errors.append(f"{origin.get('origin')}: no resource data found")
            continue
        for resource in detail.get("resources") or []:
            resources.append(_resource_summary(resource))
            for accept in resource.get("accepts") or []:
                pay_to = accept.get("payTo")
                if pay_to:
                    addresses.add(pay_to)

    result: dict[str, Any] = {
        "name": name,
        "description": description,
        "api_url": primary.get("origin"),
        "doc_url": others[0]["origin"] if others else primary.get("origin"),
        "all_urls": [o.get("origin") for o in origins],
        "resources": resources,
        "addresses": sorted(addresses, key=str.lower),
        "facilitators": group.get("facilitators") or [],
        "chains": group.get("chains") or [],
        "tx_count": group.get("tx_count"),
    }
    if errors:
        result["errors"] = errors
    return result


def scrape_all(
    limit: Optional[int] = None,
    max_workers: int = 5,
    timeout: float = 20.0,
    delay: float = 0.0,
    on_progress: Optional[Any] = None,
) -> list[dict[str, Any]]:
    groups = fetch_server_groups(timeout=timeout)
    if limit is not None:
        groups = groups[:limit]

    def worker(group: dict[str, Any]) -> dict[str, Any]:
        if delay:
            time.sleep(delay)
        result = scrape_server(group, timeout=timeout)
        if on_progress:
            on_progress()
        return result

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(worker, group) for group in groups]
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda r: (r.get("name") or "").lower())
    return results
