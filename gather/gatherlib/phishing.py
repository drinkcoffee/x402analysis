"""Which addresses are marked as phishing: those whose Blockscout name or
tags include "phish", ignoring case -- so "Phish / Hack" tags and
"Fake_Phishing..." names both count. Shared by 35_malicious_analysis.py and
36_server_usdc_received.py, which read the hand-reviewed
dataset/metadata_base_phishing_only_annotated.json (a copy of
09a_extract_phishing.py's output with entries that aren't really phishing
deleted, or their name and tags edited), and by 09a itself, which applies
the check to dataset/metadata_base.json (09_fetch_metadata_base.py).

Funding below a dust threshold is ignored: a phishing funder counts as
having funded an address only if what it sent -- USDC, plus ETH at a fixed
exchange rate -- is worth at least DUST_USD (US$0.01, i.e. 0.000004 ETH at
US$2,500 an ETH). Tiny sends are what address poisoning looks like, and
make the address sent to a target of the phishing, not funded by it.

An entry in the annotated file can have a "from" field: the date and time
after which the address is deemed phishing (say, a wallet that was
compromised), e.g. "2026-09-24T18:31:00Z" (also accepted:
"2026-09-24 18:31", "2026-09-24:18:31UTC", a date alone; UTC unless an
offset is given). Without one it's phishing at all times. Then only
transfers at or after it count: the address's own payments, and its
funding of an address -- funders.json only gives the first and last time
it sent each address, so an address counts as funded if the last send
(USDC or ETH) was at or after "from", and the dust test still uses the
total sent. An address it funded is deemed phishing from the same time.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import DATASET_DIR
from .funders import load_funders

METADATA_PATH = DATASET_DIR / "metadata_base.json"
EXTRACTED_PATH = DATASET_DIR / "metadata_base_phishing_only.json"
ANNOTATED_PATH = DATASET_DIR / "metadata_base_phishing_only_annotated.json"
WORD = "phish"
FIELDS = ("name", "tags")
DUST_USD = Decimal("0.01")
ETH_USD = Decimal("2500")
FROM_PATTERN = re.compile(
    r"\s*(\d{4}-\d{2}-\d{2})(?:[T :]+(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?"
    r"\s*(Z|UTC|GMT|[+-]\d{2}:?\d{2})?\s*",
    re.IGNORECASE,
)


def is_phishing(entry: dict) -> bool:
    """Whether a metadata_base.json entry's name or tags include WORD,
    ignoring case."""
    return any(WORD in (entry.get(field) or "").lower() for field in FIELDS)


def load_metadata() -> list[dict]:
    """ANNOTATED_PATH's "addresses" list; exits if it isn't there."""
    if not ANNOTATED_PATH.exists():
        print(
            f"dataset/{ANNOTATED_PATH.name} doesn't exist -- run 09a_extract_phishing.py, copy "
            f"dataset/{EXTRACTED_PATH.name} to it, and review it.",
            file=sys.stderr,
        )
        sys.exit(2)
    return json.loads(ANNOTATED_PATH.read_text())["addresses"]


def phishing_entries(addresses: list[dict] | None = None) -> list[dict]:
    """The metadata entries marked as phishing, sorted by address."""
    entries = load_metadata() if addresses is None else addresses
    return sorted((entry for entry in entries if is_phishing(entry)), key=lambda entry: entry["address"])


def add_dust_arguments(parser: argparse.ArgumentParser) -> None:
    """--min-funding and --eth-usd, as phishing_and_funded's arguments."""
    parser.add_argument(
        "--min-funding",
        type=Decimal,
        default=DUST_USD,
        metavar="USD",
        help=f"ignore phishing funders' sends worth less than this in all, as dust (default {DUST_USD}; 0: count all)",
    )
    parser.add_argument(
        "--eth-usd",
        type=Decimal,
        default=ETH_USD,
        metavar="USD",
        help=f"the US dollar price of an ETH used to value ETH sends (default {ETH_USD})",
    )


def funding_usd(item: dict, eth_usd: Decimal = ETH_USD) -> Decimal:
    """What a funder sent one funded address (a funders.json "funded"
    item), in US dollars: its USDC plus its ETH at `eth_usd`."""
    usdc = Decimal((item.get("usdc") or {}).get("amount") or "0")
    eth = Decimal((item.get("eth") or {}).get("amount") or "0")
    return usdc + eth * eth_usd


def is_dust(item: dict, min_usd: Decimal = DUST_USD, eth_usd: Decimal = ETH_USD) -> bool:
    """Whether what a funder sent `item` is worth less than `min_usd`."""
    return funding_usd(item, eth_usd) < min_usd


def parse_from(text: str) -> datetime:
    """An annotated entry's "from" field as an aware datetime (see the
    module's description); raises ValueError if it can't be read."""
    match = FROM_PATTERN.fullmatch(text or "")
    if not match:
        raise ValueError(f"can't read the date and time {text!r}")
    day, hour, minute, second, zone = match.groups()
    offset = timedelta(0)
    if zone and zone[0] in "+-":
        digits = zone[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        offset = -offset if zone[0] == "-" else offset
    moment = datetime.fromisoformat(day).replace(hour=int(hour or 0), minute=int(minute or 0), second=int(second or 0))
    return moment.replace(tzinfo=timezone(offset)).astimezone(timezone.utc)


def entry_from(entry: dict) -> datetime | None:
    """When `entry`'s address became phishing: its "from" field, or None
    (always). Exits, naming the address, if the field can't be read."""
    if entry.get("from") in (None, ""):
        return None
    try:
        return parse_from(entry["from"])
    except ValueError as error:
        sys.exit(f"dataset/{ANNOTATED_PATH.name}: {entry['address']}'s \"from\": {error}")


def last_sent(item: dict) -> datetime | None:
    """When a funder last sent a funded address (a funders.json "funded"
    item) USDC or ETH, or None if neither has a time."""
    times = [(item.get(asset) or {}).get("last") for asset in ("usdc", "eth")]
    times = [datetime.fromisoformat(time.replace("Z", "+00:00")) for time in times if time]
    return max(times) if times else None


def funded_after(item: dict, since: datetime | None) -> bool:
    """Whether a funder that became phishing at `since` (None: always)
    sent `item` anything then or later."""
    if since is None:
        return True
    last = last_sent(item)
    return last is not None and last >= since


def earliest(a: datetime | None, b: datetime | None) -> datetime | None:
    """The earlier of two "phishing from" times, None (always) being
    earliest."""
    return None if a is None or b is None else min(a, b)


def phishing_and_funded(
    min_usd: Decimal = DUST_USD, eth_usd: Decimal = ETH_USD
) -> tuple[dict[str, datetime | None], dict[str, datetime | None]]:
    """(the addresses marked as phishing, the addresses phishing funders
    funded with at least `min_usd` at or after the funder's "from" -- from
    dataset/funders.json or its pieces), lower case, each with when it's
    deemed phishing from (None: always; for a funded address, the earliest
    "from" of the phishing funders that funded it)."""
    phishing = {entry["address"].lower(): entry_from(entry) for entry in phishing_entries()}
    funded: dict[str, datetime | None] = {}
    for funder in load_funders():
        address = funder["address"].lower()
        if address not in phishing:
            continue
        since = phishing[address]
        for item in funder.get("funded") or []:
            if is_dust(item, min_usd, eth_usd) or not funded_after(item, since):
                continue
            key = item["address"].lower()
            funded[key] = earliest(funded[key], since) if key in funded else since
    return phishing, funded
