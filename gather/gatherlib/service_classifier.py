# Copied from x402-analysis-api/scripts/service_classifier.py so gather doesn't depend on code in other directories.
"""Classifies x402 servers' resources (endpoints) into categories based on
keyword matches in each resource's own description, URL, and tags.

Reads a servers JSON file (see tempdata/x402scansServers.json for the shape:
a list of objects with resources[].{description,url,tags}, among other
top-level fields that are ignored for classification) and assigns each
resource, independently, to the first category in CATEGORY_RULES whose
include terms are found in that resource's own description/url/tags and
whose exclude terms are not. A term in one resource never affects another
resource's classification, including other resources on the same server.
Resources matching no rule land in "uncategorised".

Matching is a plain case-insensitive substring search (everything is
lowercased first), not word-boundary aware -- e.g. the "ip " term for the
network category also matches inside words like "...ship " or domains like
".vip"/".rip" once a resource's own description/url/tags are squashed into
one string, which is what a category's exclude terms are for.

Add more categories, or more include/exclude terms to an existing one, by
editing CATEGORY_RULES below.

Vendored, byte-for-byte, from the sibling cli-tool project's
service_classifier.py, so update.py doesn't need cli-tool checked out next
to this project to categorise services -- it imports classify_resource()
and resource_text() directly; the CLI below (for classifying a standalone
servers JSON file) is carried along unused for the same reason cdp_client.py
and x402scan_scraper.py keep theirs: to stay an exact copy of the source.

Usage:
    python3 server_classifier.py <servers.json> [-o output.txt]

Prints a category/resource-count table to stdout, and writes a detailed
report (one section per category, listing each matched resource and the
service it belongs to) to the output file (default: <input stem>-categories.txt
next to the input file).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CategoryRule:
    name: str
    include: list[str]
    exclude: list[str] = field(default_factory=list)


# Checked in order; a resource goes to the first category whose include terms
# match and whose exclude terms don't. Anything left over is "uncategorised".
CATEGORY_RULES: list[CategoryRule] = [
    CategoryRule(
        "service-inactive",
        include=["api.zaroniacalculator.co.za/v1/verify/frn",
                 "api.24klabs.ai/api",
                 "x402pastebin.987659876.xyz",
                 "api.xona-agent.com",
                 "gift_recommender", "gift finder"],
    ),
    CategoryRule(
        "*giftcard", 
        include=["gift"],
        exclude=["gift_certificate", 
                 "dealpulse.theaslangroupllc.com/api/deals/giftcard",
                 "dealpulse.theaslangroupllc.com/api/deals/subscription-letter"]
    ),
    CategoryRule(
        "*password",
        include=["password"],
        exclude=[],
    ),
    CategoryRule(
        "*secret",
        include=["secret",
                 "pastebin.0000402.xyz"],
        exclude=["non-secret", "secretary"],
    ),
    CategoryRule(
        "*pii",
        include=["pii"],
    ),
    CategoryRule(
        "*gambling",
        include=["play.0000402.xyz"],
    ),
    CategoryRule(
        "*image-to-be-sorted",
        include=["image", "video", "mp4"],
        exclude=["search", "ocr", "exif", "written", "format", "colors", "qr", "background", "audio",
                 "markdown", "performance", "hailuo", "youtube", "banana", "description", "alt-",
                 "downloader", "convert", "resize", "ad-",
                 "face-swap"]
    ),
    CategoryRule(
        "*malicious-image",
        include=["image", "video", "mp4"],
        exclude=["search", "ocr", "exif", "written", "format", "colors", "qr", "background", "audio",
                 "markdown", "performance", "hailuo", "youtube", "banana","description", "alt-", 
                 "downloader", "convert", "resize", "ad-",
                 ]
    ),
    CategoryRule(
        "image",
        include=["image", "video", "mp4"],
    ),
    CategoryRule(
        "network",
        include=["-ip ", " ip ", "/ip-", "domain", "tld", "ipv", "4byte"],
        # "ip " also turns up inside words like "...ship " and TLDs like
        # ".vip"/".rip" -- exclude those false positives.
        exclude=["hip", "rip", "vip", "rip", "tip", "lip"],
    ),
    CategoryRule(
        "blockchain",
        include=["ethereum", "arbitrum", "optimism", "token", "abi", "bitcoin", "crypto", "btc", 
                 "defi", "dex", "perpetual", "order-book", "gas", "solana", "ens", 
                 "uniswap", "pancake", "sushi", "evm", "yield", "wallet", "tx-subscription", 
                 "0x.org", "asset", "signature", "signed", "liquidity"],
        exclude=[],
    ),
    CategoryRule(
        "news",
        include=["news"],
        exclude=[],
    ),
    CategoryRule(
        "shopping",
        include=["ebay", "amazon", "costco", "product", "shop", "gift finder",
                 "dealpulse.theaslangroupllc.com/api/deals/giftcard",
                 "punksinapunk.0000402.xyz", "canvas.0000402.xyz"],
        exclude=[],
    ),
    CategoryRule(
        "job-search",
        include=["job", "salary"],
        exclude=[],
    ),
    CategoryRule(
        "software-dev",
        include=["git", "database migrations", "database schema", "json", "x402 ecosystem", 
                 "x402 seller", "x402 network", "nlp/", "md/", "palette", "wiki", "stack", 
                 "scrape", "screenshot", "cron", "base64", "web search", "geocode", "convert",
                 "morse", "uuid", "regex", "rgb", "encoder", "avatar", "cldr", "pdf", "ipfs",
                 "dns", "webhook", "keyword-frequency", "markdown", "jwt", "tls", "rot13",
                 "robots-check", "seo"],
        exclude=[".json"],
    ),
    CategoryRule(
        "health",
        include=["hospital", "Medicare", "healthcare", "nutrition", "herb", "dental", "cosmetic", 
                 "sleep", "mind", "fengshui", "astrology"],
    ),
    CategoryRule(
        "researcher",
        include=["researcher", "citation", "paper", "patent",
                 "https://jurat.dev/v1/snapshot/batch"],
    ),
    CategoryRule(
        "weather",
        include=["weather", "precipitation", "forecast"],
    ),
    CategoryRule(
        "finance",
        include=["wealth", "vet/", "startup", "equit", "commodit", "esg", "rental", "debt", "roi", 
                 "perp", "etf", "quote", "taker", "percentage", "interest", "exchange", "rate",
                 "statistics", "company", "stock", "market", "filing", "gdp", "macro", "payroll",
                 "cpi", "inflation", "contract award", "companies", "ein", "duns", "cusip", "abn",
                 "economic", "iban", "vat", "tender", "amortization", "fx", "ipo", "loan", "recession",
                 "earnings", "gift_certificate", "secretary", 
                 "dealpulse.theaslangroupllc.com/api/deals/subscription-letter"],
    ),
    CategoryRule(
        "social-media",
        include=["linkedin", "instagram", "facebook", "rss", "podcast", "twitter", "twitr.sh", "tweet"],
    ),
    CategoryRule(
        "util",
        include=["time", "business day", "formataddress.com", "public holiday", "holidays", "roman numerals"],
    ),
    CategoryRule(
        "llm",
        include=["llm", "embedding", "code-run", "x402audit.dev/api/audit", "unpaid x402 probe", 
                 "/ai/", "chunk", "text-analyze", "anthropic", "openai"],
    ),
    CategoryRule(
        "sport",
        include=["sport", "soccer"],
    ),
    CategoryRule(
        "travel",
        include=["hotel", "flight", "trip", "track", "rail"],
    ),
    CategoryRule(
        "search",
        include=["search"],
    ),
    CategoryRule(
        "notorization",
        include=["usaref.dev/v1/all", "jurat.dev/v1/conformance"],
    ),


    
]

UNCATEGORISED = "uncategorised"


def resource_text(resource: dict[str, Any]) -> str:
    """A single resource's description/url/tags fields, lowercased and
    joined into one string for keyword matching. Top-level server fields
    (name, description, api_url, doc_url, all_urls) are not searched."""
    parts = [resource.get("description") or "", resource.get("url") or ""]
    parts.extend(resource.get("tags") or [])
    return " ".join(parts).lower()


def classify_resource(text: str) -> str:
    for rule in CATEGORY_RULES:
        matches = any(term in text for term in rule.include)
        excluded = any(term in text for term in rule.exclude)
        if matches and not excluded:
            # print(f"{rule.name}: {text}")
            return rule.name
    return UNCATEGORISED


def print_table(headers: list[str], rows: list[list[Any]]) -> None:
    str_rows = [[str(cell) for cell in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: list[str]) -> str:
        return "  ".join(cell.ljust(w) for cell, w in zip(cells, widths))

    print(fmt(list(headers)))
    print(fmt(["-" * w for w in widths]))
    for row in str_rows:
        print(fmt(row))


def write_report(
    path: Path, categorised: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]]
) -> None:
    lines: list[str] = []
    for category, entries in categorised.items():
        lines.append(f"=== {category} ({len(entries)}) ===")
        for server, resource in entries:
            lines.append(f"- {server.get('name')}")
            url = resource.get("url") or ""
            if url:
                lines.append(f"    url: {url}")
            description = (resource.get("description") or "").strip()
            if description:
                lines.append(f"    description: {description}")
            tags = resource.get("tags") or []
            if tags:
                lines.append(f"    tags: {', '.join(tags)}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify x402 servers by keyword.")
    parser.add_argument("input", help="path to a servers JSON file")
    parser.add_argument(
        "-o",
        "--output",
        help="path to write the detailed report to (default: <input stem>-categories.txt)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    servers = json.loads(input_path.read_text())
    if not isinstance(servers, list):
        raise SystemExit(f"expected {input_path} to contain a JSON list of servers")

    output_path = (
        Path(args.output)
        if args.output
        else input_path.with_name(f"{input_path.stem}-categories.txt")
    )

    categorised: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {
        rule.name: [] for rule in CATEGORY_RULES
    }
    categorised[UNCATEGORISED] = []
    for server in servers:
        for resource in server.get("resources") or []:
            category = classify_resource(resource_text(resource))
            categorised[category].append((server, resource))

    print_table(
        ["category", "services"],
        [[name, len(items)] for name, items in categorised.items()],
    )

    write_report(output_path, categorised)
    print(f"\nwrote detailed report to {output_path}")


if __name__ == "__main__":
    main()
