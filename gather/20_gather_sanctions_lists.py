#!/usr/bin/env python3
"""Downloads the latest US and Australian sanctions lists and combines them
into dataset/sanctions_list.json and gather's database
(sanctions_entries, sanctions_addresses).

Lists:
  - OFAC's Specially Designated Nationals (SDN) list, as XML -- the
    download offered at https://sanctionslist.ofac.treas.gov/Home/SdnList.
    Blockchain addresses are listed as IDs of type "Digital Currency
    Address - <currency>" (e.g. XBT, ETH, USDT, TRX, SOL).
  - Australia's consolidated list (DFAT), as a spreadsheet with a row per
    name: a primary name, then aliases and original-script names whose
    references are the primary's plus a letter (2578, 2578a, 2578b). Those
    rows are combined into one entry. It currently lists no blockchain
    addresses.

Every entry from both lists is kept -- people, entities, vessels and
aircraft, with or without addresses -- each labelled with its list
("OFAC SDN" or "AU DFAT"). Someone on both lists gets an entry for each:
names are spelled differently between the lists, so they aren't merged.
Ethereum-style (0x...) addresses are stored in lower case; others as
listed, since base58 and bech32 addresses are case-sensitive.

Unlike 01-09, this can be (and should be) re-run to pick up list updates:
each run replaces dataset/sanctions_list.json and everything in the two
tables, in one transaction, so a failed or interrupted run changes
nothing. The downloaded files are kept in localdb/sanctions/.

Usage:
    python gather/20_gather_sanctions_lists.py
"""

from __future__ import annotations

import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

GATHER_ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    import openpyxl
    import psycopg2  # noqa: F401
    import psycopg2.extras
    import requests
except ImportError as exc:
    print(
        f"missing required package: {exc.name}. Run `pip install -r gather/requirements.txt`, "
        "with gather's virtualenv active.",
        file=sys.stderr,
    )
    sys.exit(1)

load_dotenv(GATHER_ROOT / ".env")

from gatherlib import DATASET_DIR, localdb, preflight  # noqa: E402
from gatherlib.common import ETH_ADDRESS, write_json  # noqa: E402

OUTPUT_PATH = DATASET_DIR / "sanctions_list.json"
DOWNLOAD_DIR = localdb.LOCAL_DB_ROOT / "sanctions"

OFAC_LIST = "OFAC SDN"
OFAC_PAGE = "https://sanctionslist.ofac.treas.gov/Home/SdnList"
OFAC_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"
AU_LIST = "AU DFAT"
AU_URL = "https://www.dfat.gov.au/sites/default/files/Australian_Sanctions_Consolidated_List.xlsx"

DIGITAL_CURRENCY_PREFIX = "Digital Currency Address - "
SEPARATOR = "; "
TIMEOUT = 120.0
MAX_ATTEMPTS = 4
RETRY_WAIT = 15.0
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"}


def download(url: str, path: Path) -> Path:
    """Downloads `url` to `path`, retrying server errors and network
    failures."""
    print(f"downloading {url} ...", flush=True)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as exc:
            problem = type(exc).__name__
        else:
            if response.ok:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(response.content)
                print(f"  {len(response.content):,} bytes", flush=True)
                return path
            if response.status_code != 429 and response.status_code < 500:
                raise RuntimeError(f"{url}: HTTP {response.status_code}")
            problem = f"HTTP {response.status_code}"
        if attempt < MAX_ATTEMPTS:
            wait = RETRY_WAIT * attempt
            print(f"  {problem} -- retrying in {wait:.0f}s", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"{url}: still failing after {MAX_ATTEMPTS} attempts ({problem})")


def blockchain_address(currency: str, address: str) -> dict:
    address = address.strip()
    if ETH_ADDRESS.match(address):
        address = address.lower()
    return {"currency": currency.strip(), "address": address}


def _joined(*parts) -> str | None:
    return ", ".join(p.strip() for p in parts if p and p.strip()) or None


def _dedup(values) -> list:
    return list(dict.fromkeys(v for v in values if v))


# --- OFAC SDN -------------------------------------------------------------


def parse_ofac(path: Path) -> tuple[str | None, list[dict]]:
    """(publish date, entries) from SDN.XML."""
    root = ET.parse(path).getroot()
    ns = root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""

    def text(element, tag: str) -> str | None:
        value = element.findtext(ns + tag)
        return value.strip() if value and value.strip() else None

    def items(element, path: str):
        return element.findall("/".join(ns + part for part in path.split("/")))

    def ofac_name(element) -> str:
        # OFAC writes names "LAST, First"; entities have only a lastName.
        return ", ".join(p for p in (text(element, "lastName"), text(element, "firstName")) if p)

    published = root.findtext(f"{ns}publshInformation/{ns}Publish_Date")
    if published:
        try:
            published = datetime.strptime(published.strip(), "%m/%d/%Y").date().isoformat()
        except ValueError:
            published = published.strip()

    entries = []
    for e in items(root, "sdnEntry"):
        addresses, ids = [], []
        for id_ in items(e, "idList/id"):
            id_type = text(id_, "idType") or ""
            number = text(id_, "idNumber")
            if id_type.startswith(DIGITAL_CURRENCY_PREFIX) and number:
                addresses.append(blockchain_address(id_type[len(DIGITAL_CURRENCY_PREFIX) :], number))
            elif number:
                ids.append(
                    {
                        k: v
                        for k, v in {
                            "type": id_type.rstrip(" :-") or None,
                            "number": number,
                            "country": text(id_, "idCountry"),
                            "issued": text(id_, "issueDate"),
                            "expires": text(id_, "expirationDate"),
                        }.items()
                        if v
                    }
                )
        details = {
            "title": text(e, "title"),
            "remarks": text(e, "remarks"),
            "dates_of_birth": [text(d, "dateOfBirth") for d in items(e, "dateOfBirthList/dateOfBirthItem")],
            "places_of_birth": [text(p, "placeOfBirth") for p in items(e, "placeOfBirthList/placeOfBirthItem")],
            "nationalities": [text(n, "country") for n in items(e, "nationalityList/nationality")],
            "citizenships": [text(c, "country") for c in items(e, "citizenshipList/citizenship")],
            "addresses": [
                _joined(*(text(a, t) for t in ("address1", "address2", "address3", "city", "stateOrProvince", "postalCode", "country")))
                for a in items(e, "addressList/address")
            ],
            "ids": ids,
        }
        vessel = e.find(ns + "vesselInfo")
        if vessel is not None:
            details["vessel"] = {child.tag[len(ns) :]: child.text.strip() for child in vessel if child.text and child.text.strip()}
        entries.append(
            {
                "list": OFAC_LIST,
                "list_id": text(e, "uid"),
                "name": ofac_name(e),
                "type": (text(e, "sdnType") or "").lower() or None,
                "aliases": _dedup(ofac_name(a) for a in items(e, "akaList/aka")),
                "programs": _dedup(p.text.strip() for p in items(e, "programList/program") if p.text),
                "blockchain_addresses": _dedup_addresses(addresses),
                "details": _compact(details),
            }
        )
    return published, entries


def _dedup_addresses(addresses: list[dict]) -> list[dict]:
    seen, unique = set(), []
    for a in addresses:
        key = (a["currency"], a["address"])
        if key not in seen:
            seen.add(key)
            unique.append(a)
    return unique


def _compact(details: dict) -> dict:
    """`details` without empty values, and with duplicate list items
    removed."""
    compact = {}
    for key, value in details.items():
        if isinstance(value, list):
            value = _dedup(value) if all(isinstance(v, str) or v is None for v in value) else value
        if value:
            compact[key] = value
    return compact


# --- Australia (DFAT) -----------------------------------------------------

AU_COLUMNS = {
    "reference": "Reference",
    "name": "Name of Individual or Entity",
    "type": "Type",
    "name_type": "Name Type",
    "date_of_birth": "Date of Birth",
    "place_of_birth": "Place of Birth",
    "citizenship": "Citizenship",
    "address": "Address",
    "additional_information": "Additional Information",
    "listing_information": "Listing Information",
    "imo_number": "IMO Number",
    "committees": "Committees",
    "control_date": "Control Date",
    "instrument": "Instrument of Designation",
    "targeted_financial_sanction": "Targeted Financial Sanction",
    "travel_ban": "Travel Ban",
    "arms_embargo": "Arms Embargo",
    "maritime_restriction": "Maritime Restriction",
}
AU_DETAILS = (
    "date_of_birth",
    "place_of_birth",
    "citizenship",
    "address",
    "additional_information",
    "listing_information",
    "imo_number",
    "control_date",
    "instrument",
    "targeted_financial_sanction",
    "travel_ban",
    "arms_embargo",
    "maritime_restriction",
)


def _cell(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value.strip() or None
    return value


def parse_au(path: Path) -> tuple[str | None, list[dict]]:
    """(list date, entries) from the consolidated list spreadsheet."""
    sheet = openpyxl.load_workbook(path, read_only=True).worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    header = [str(h).strip() if h else "" for h in next(rows)]
    missing = [title for title in AU_COLUMNS.values() if title not in header]
    if missing:
        raise RuntimeError(f"the Australian list's columns have changed -- missing {missing}")
    index = {key: header.index(title) for key, title in AU_COLUMNS.items()}

    # The sheet is named e.g. "Consolidated List - 7_10_2026" (day_month_year).
    published = None
    match = re.search(r"(\d{1,2})_(\d{1,2})_(\d{4})", sheet.title)
    if match:
        day, month, year = map(int, match.groups())
        published = date(year, month, day).isoformat()

    groups: dict[str, list[dict]] = {}
    for values in rows:
        row = {key: _cell(values[i]) if i < len(values) else None for key, i in index.items()}
        if row["reference"] is None or not row["name"]:
            continue
        # An alias's reference is its primary's plus a letter suffix.
        reference = re.sub(r"[A-Za-z]+$", "", str(row["reference"]).strip())
        groups.setdefault(reference, []).append(row)

    entries = []
    for reference, group in groups.items():
        primary = next((r for r in group if r["name_type"] == "Primary Name"), group[0])
        entries.append(
            {
                "list": AU_LIST,
                "list_id": reference,
                "name": primary["name"],
                "type": (primary["type"] or "").lower() or None,
                "aliases": _dedup(r["name"] for r in group if r is not primary and r["name"] != primary["name"]),
                "programs": _dedup(c.strip() for c in (primary["committees"] or "").split(";")),
                "blockchain_addresses": [],
                "details": _compact({key: primary[key] for key in AU_DETAILS if primary[key] not in (None, False)}),
            }
        )
    return published, entries


# --- Storing --------------------------------------------------------------


def store(cur, entries: list[dict]) -> None:
    """Replaces everything in sanctions_entries and sanctions_addresses
    with `entries`."""
    cur.execute("DELETE FROM sanctions_addresses")
    cur.execute("DELETE FROM sanctions_entries")
    ids = psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO sanctions_entries (list, list_id, name, type, aliases, programs, details)
        VALUES %s
        RETURNING id
        """,
        [
            (
                e["list"],
                e["list_id"],
                e["name"],
                e["type"],
                SEPARATOR.join(e["aliases"]) or None,
                SEPARATOR.join(e["programs"]) or None,
                psycopg2.extras.Json(e["details"]) if e["details"] else None,
            )
            for e in entries
        ],
        page_size=1000,
        fetch=True,
    )
    psycopg2.extras.execute_values(
        cur,
        "INSERT INTO sanctions_addresses (entry, currency, address) VALUES %s",
        [
            (entry_id, a["currency"], a["address"])
            for (entry_id,), e in zip(ids, entries)
            for a in e["blockchain_addresses"]
        ],
        page_size=1000,
    )


def main() -> None:
    preflight.check_or_exit(localdb.preflight_checks)
    conn = localdb.connect_existing()
    try:
        with conn.cursor() as cur:
            localdb.ensure_schema(cur)
        conn.commit()
    finally:
        conn.close()

    try:
        ofac_published, ofac = parse_ofac(download(OFAC_URL, DOWNLOAD_DIR / "SDN.XML"))
        au_published, au = parse_au(download(AU_URL, DOWNLOAD_DIR / "Australian_Sanctions_Consolidated_List.xlsx"))
    except (requests.RequestException, RuntimeError, ET.ParseError) as exc:
        print(f"couldn't get the sanctions lists: {exc} -- nothing changed", file=sys.stderr)
        sys.exit(1)

    # Entries with blockchain addresses first, each list in its own order.
    entries = sorted(ofac + au, key=lambda e: not e["blockchain_addresses"])
    sources = []
    for name, page, url, published, list_entries in (
        (OFAC_LIST, OFAC_PAGE, OFAC_URL, ofac_published, ofac),
        (AU_LIST, None, AU_URL, au_published, au),
    ):
        with_addresses = [e for e in list_entries if e["blockchain_addresses"]]
        currencies = Counter(a["currency"] for e in with_addresses for a in e["blockchain_addresses"])
        sources.append(
            {
                "list": name,
                **({"page": page} if page else {}),
                "url": url,
                "published": published,
                "entries": len(list_entries),
                "entries_with_addresses": len(with_addresses),
                "blockchain_addresses": sum(currencies.values()),
                "by_currency": dict(currencies.most_common()),
            }
        )
        print(
            f"{name} (published {published or 'date unknown'}): {len(list_entries):,} entries, "
            f"{len(with_addresses):,} with {sum(currencies.values()):,} blockchain address(es)"
        )

    try:
        conn = localdb.connect_existing()
        try:
            with conn.cursor() as cur:
                store(cur, entries)
            conn.commit()
        finally:
            conn.close()
    except KeyboardInterrupt:
        print("\ninterrupted -- nothing changed", file=sys.stderr)
        sys.exit(130)

    write_json(
        OUTPUT_PATH,
        {
            "gathered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sources": sources,
            "entries": entries,
        },
    )
    print(
        f"stored {len(entries):,} entries in sanctions_entries and "
        f"{sum(len(e['blockchain_addresses']) for e in entries):,} address(es) in sanctions_addresses; "
        f"wrote {OUTPUT_PATH.relative_to(GATHER_ROOT)}"
    )


if __name__ == "__main__":
    main()
