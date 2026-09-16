"""x402tool command-line interface."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

import requests

from . import blockchain_addresses, cloudbric_threatdb, net_analysis, registry
from .cdp_client import CdpClient, CdpClientError
from .formatting import print_error, print_response, print_table, response_body
from .generic_client import GenericFacilitatorClient

DEFAULT_TIMEOUT = 30.0


# --------------------------------------------------------------------------
# Small argument-parsing helpers
# --------------------------------------------------------------------------


def _read_json_source(source: str) -> Any:
    if source == "-":
        text = sys.stdin.read()
    else:
        with open(source, encoding="utf-8") as fh:
            text = fh.read()
    return json.loads(text)


def _parse_header(raw: str) -> tuple[str, str]:
    if ":" not in raw:
        raise argparse.ArgumentTypeError(f"--header expects 'Name: Value', got: {raw!r}")
    name, _, value = raw.partition(":")
    return name.strip(), value.strip()


def _add_common_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print raw {status, body} JSON")


def _add_generic_auth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--api-key",
        help="API key/secret for facilitators that need one (see `x402tool show <id>`)",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        type=_parse_header,
        metavar="'Name: Value'",
        help="extra HTTP header to send; may be repeated",
    )
    parser.add_argument("--url", help="override the facilitator's base URL")


def _add_cdp_auth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-key-id", help="CDP API key ID (default: $CDP_API_KEY_ID)")
    parser.add_argument("--api-key-secret", help="CDP API key secret (default: $CDP_API_KEY_SECRET)")


def _add_payment_body_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--body",
        metavar="FILE",
        help="JSON file (or '-' for stdin) containing the full request body: "
        "{x402Version, paymentPayload, paymentRequirements}",
    )
    parser.add_argument(
        "--payload",
        metavar="FILE",
        help="JSON file (or '-' for stdin) containing paymentPayload; used with --requirements",
    )
    parser.add_argument(
        "--requirements",
        metavar="FILE",
        help="JSON file (or '-' for stdin) containing paymentRequirements; used with --payload",
    )
    parser.add_argument(
        "--x402-version",
        type=int,
        default=2,
        choices=(1, 2),
        help="x402 protocol version when using --payload/--requirements (default: 2)",
    )


def _load_payment_body(args: argparse.Namespace) -> tuple[int, dict[str, Any], dict[str, Any]] | None:
    if args.body:
        data = _read_json_source(args.body)
        try:
            return data["x402Version"], data["paymentPayload"], data["paymentRequirements"]
        except KeyError as exc:
            print_error(f"--body JSON is missing required field: {exc}")
            return None
    if args.payload and args.requirements:
        payload = _read_json_source(args.payload)
        requirements = _read_json_source(args.requirements)
        return args.x402_version, payload, requirements
    print_error("provide either --body, or both --payload and --requirements")
    return None


# --------------------------------------------------------------------------
# Facilitator resolution: registry entry, ad hoc URL, or CDP
# --------------------------------------------------------------------------


def _resolve_target(args: argparse.Namespace):
    """Return either a GenericFacilitatorClient or a CdpClient, with a label."""
    extra_headers = dict(args.header)
    api_key = args.api_key or os.environ.get("X402_API_KEY")
    identifier = args.facilitator

    if args.url:
        client = GenericFacilitatorClient(
            args.url, headers=extra_headers, timeout=args.timeout, verbose=args.verbose
        )
        return client, identifier or args.url

    if identifier.startswith("http://") or identifier.startswith("https://"):
        client = GenericFacilitatorClient(
            identifier, headers=extra_headers, timeout=args.timeout, verbose=args.verbose
        )
        return client, identifier

    try:
        entry = registry.get(identifier)
    except registry.UnknownFacilitatorError as exc:
        print_error(str(exc))
        return None, None

    if entry["api"] == "cdp":
        client = CdpClient(
            key_id=args.api_key_id or os.environ.get("CDP_API_KEY_ID"),
            key_secret=args.api_key_secret or os.environ.get("CDP_API_KEY_SECRET"),
            timeout=args.timeout,
            verbose=args.verbose,
        )
        return client, entry["name"]

    base_url, headers = registry.resolve_generic_target(entry, api_key=api_key, extra_headers=extra_headers)
    client = GenericFacilitatorClient(base_url, headers=headers, timeout=args.timeout, verbose=args.verbose)
    return client, entry["name"]


# --------------------------------------------------------------------------
# Command implementations
# --------------------------------------------------------------------------


def _short_networks(networks: list[str], limit: int = 4) -> str:
    names = [n.split(" ")[0] for n in networks]
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f", +{len(names) - limit} more"


def _list_facilitators(args: argparse.Namespace, entries: dict[str, Any]) -> int:
    rows = []
    for fid in sorted(entries):
        entry = entries[fid]
        if args.access and entry["access"] != args.access:
            continue
        if args.network and not any(args.network.lower() in n.lower() for n in entry["networks"]):
            continue
        rows.append((fid, entry))

    if args.json:
        print(json.dumps({fid: entry for fid, entry in rows}, indent=2, default=str))
        return 0

    print_table(
        ["ID", "NAME", "API", "ACCESS", "NETWORKS", "BASE URL"],
        [
            (fid, entry["name"], entry["api"], entry["access"], _short_networks(entry["networks"]), entry["base_url"])
            for fid, entry in rows
        ],
    )
    print(f"\n{registry.SOURCE_NOTE}")
    return 0


def cmd_list_facilitators(args: argparse.Namespace) -> int:
    return _list_facilitators(args, registry.FACILITATORS)


def cmd_list_defunct_facilitators(args: argparse.Namespace) -> int:
    return _list_facilitators(args, registry.DEFUNCT_FACILITATORS)


_LIST_FAC_DOMAINS_CSV_COLUMNS = ("name", "type", "url")


def cmd_list_fac_domains(args: argparse.Namespace) -> int:
    writer = csv.writer(sys.stdout)
    writer.writerow(_LIST_FAC_DOMAINS_CSV_COLUMNS)
    for fid in sorted(registry.FACILITATORS, key=lambda i: registry.FACILITATORS[i]["name"].lower()):
        entry = registry.FACILITATORS[fid]
        name = entry["name"]
        base_url = entry["base_url"]
        docs_url = entry["docs_url"]
        writer.writerow([name, "API", base_url])
        writer.writerow([name, "doc", "same" if docs_url == base_url else docs_url])
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        entry = registry.get(args.facilitator)
    except registry.UnknownFacilitatorError as exc:
        return print_error(str(exc))

    if args.json:
        print(json.dumps(entry, indent=2, default=str))
        return 0

    print(f"{args.facilitator}: {entry['name']}")
    print(f"  api:        {entry['api']}")
    print(f"  base url:   {entry['base_url']}")
    print(f"  docs:       {entry['docs_url']}")
    print(f"  access:     {entry['access']}")
    print(f"  fee:        {entry['fee']}")
    print(f"  networks:   {', '.join(entry['networks'])}")
    auth = entry.get("auth", {})
    print(f"  auth:       {auth.get('type')}")
    if entry.get("notes"):
        print(f"  notes:      {entry['notes']}")
    return 0


def _print_payment_required(body: dict[str, Any]) -> None:
    """Print an x402 402-response body: {x402Version, accepts[], resource?, error?, extensions?}."""
    print(f"x402Version: {body.get('x402Version', 'n/a')}")

    resource = body.get("resource")
    if isinstance(resource, dict):
        print(f"resource:    {resource.get('url', 'n/a')}")
        if resource.get("description"):
            print(f"description: {resource['description']}")
        if resource.get("mimeType"):
            print(f"mime type:   {resource['mimeType']}")

    if body.get("error"):
        print(f"error:       {body['error']}")

    extensions = body.get("extensions")
    if extensions:
        keys = ", ".join(extensions.keys()) if isinstance(extensions, dict) else str(extensions)
        print(f"extensions:  {keys}")

    accepts = body.get("accepts") or []
    print(f"\n{len(accepts)} accepted payment option(s):")
    for i, opt in enumerate(accepts, 1):
        print(f"\n  [{i}] scheme={opt.get('scheme')}  network={opt.get('network')}")
        amount = opt.get("amount", opt.get("maxAmountRequired"))
        print(f"      amount:          {amount}")
        print(f"      asset:           {opt.get('asset')}")
        print(f"      pay to:          {opt.get('payTo')}")
        print(f"      max timeout (s): {opt.get('maxTimeoutSeconds')}")
        if opt.get("description"):
            print(f"      description:     {opt['description']}")
        if opt.get("mimeType"):
            print(f"      mime type:       {opt['mimeType']}")
        extra = opt.get("extra")
        if extra:
            print(f"      extra:           {json.dumps(extra)}")


def cmd_request(args: argparse.Namespace) -> int:
    headers = dict(args.header)
    try:
        response = requests.request(args.method, args.url, headers=headers, timeout=args.timeout)
    except requests.exceptions.RequestException as exc:
        return print_error(f"request failed: {exc}")

    body = response_body(response)

    if args.json:
        print(json.dumps({"status": response.status_code, "body": body}, indent=2, default=str))
        return 0 if response.status_code == 402 else 1

    if response.status_code != 402:
        print_error(f"expected HTTP 402 Payment Required, got HTTP {response.status_code}")
        if isinstance(body, (dict, list)):
            print(json.dumps(body, indent=2, default=str))
        else:
            print(body)
        return 1

    if not isinstance(body, dict):
        print_error("402 response body was not JSON")
        print(body)
        return 1

    print(f"HTTP 402 Payment Required - {args.url}\n")
    _print_payment_required(body)
    return 0


def cmd_supported(args: argparse.Namespace) -> int:
    client, label = _resolve_target(args)
    if client is None:
        return 1
    try:
        response = client.supported()
    except CdpClientError as exc:
        return print_error(str(exc))
    return print_response(response, args.json, label=f"{label} /supported")


def cmd_verify(args: argparse.Namespace) -> int:
    client, label = _resolve_target(args)
    if client is None:
        return 1
    body = _load_payment_body(args)
    if body is None:
        return 1
    x402_version, payload, requirements = body
    try:
        response = client.verify(x402_version, payload, requirements)
    except CdpClientError as exc:
        return print_error(str(exc))
    return print_response(response, args.json, label=f"{label} /verify")


def cmd_settle(args: argparse.Namespace) -> int:
    client, label = _resolve_target(args)
    if client is None:
        return 1
    body = _load_payment_body(args)
    if body is None:
        return 1
    x402_version, payload, requirements = body
    try:
        response = client.settle(x402_version, payload, requirements)
    except CdpClientError as exc:
        return print_error(str(exc))
    return print_response(response, args.json, label=f"{label} /settle")


def _cdp_client_from_args(args: argparse.Namespace) -> CdpClient:
    return CdpClient(
        key_id=args.api_key_id or os.environ.get("CDP_API_KEY_ID"),
        key_secret=args.api_key_secret or os.environ.get("CDP_API_KEY_SECRET"),
        timeout=args.timeout,
        verbose=args.verbose,
    )


def cmd_cdp_discover_resources(args: argparse.Namespace) -> int:
    client = _cdp_client_from_args(args)
    response = client.discovery_resources(type_=args.type, limit=args.limit, offset=args.offset)
    return print_response(response, args.json, label="CDP /discovery/resources")


_DISCOVER_RESOURCES_CSV_COLUMNS = (
    "amount",
    "asset",
    "network",
    "payTo",
    "l30DaysTotalCalls",
    "l30DaysUniquePayers",
    "description",
    "resource",
    "receiverAuthorizer",
    "scheme",
)


def cmd_cdp_discover_resources_csv(args: argparse.Namespace) -> int:
    client = _cdp_client_from_args(args)
    response = client.discovery_resources(type_=args.type, limit=args.limit, offset=args.offset)
    body = response_body(response)
    if not response.ok:
        return print_error(f"CDP /discovery/resources returned HTTP {response.status_code}: {body}")

    items = body.get("items", []) if isinstance(body, dict) else []
    writer = csv.writer(sys.stdout)
    writer.writerow(_DISCOVER_RESOURCES_CSV_COLUMNS)
    for item in items:
        description = item.get("description", "")
        resource = item.get("resource", "")
        quality = item.get("quality") or {}
        l30_days_total_calls = quality.get("l30DaysTotalCalls", "")
        l30_days_unique_payers = quality.get("l30DaysUniquePayers", "")
        for accept in item.get("accepts", []):
            extra = accept.get("extra") or {}
            writer.writerow(
                [
                    accept.get("amount", ""),
                    accept.get("asset", ""),
                    accept.get("network", ""),
                    accept.get("payTo", ""),
                    l30_days_total_calls,
                    l30_days_unique_payers,
                    description,
                    resource,
                    extra.get("receiverAuthorizer", ""),
                    accept.get("scheme", ""),
                ]
            )
    return 0


_DISCOVER_RESOURCES_CSV2_RATE_SECONDS = 1.0


def cmd_cdp_discover_resources_csv2(args: argparse.Namespace) -> int:
    """Like `cdp discover-resources-csv`, but pages through the *entire*
    result set: after each call it counts the resources returned and moves
    the offset forward by that count, rate-limited to one call per second,
    printing one "." per call so a long download stays visible."""
    client = _cdp_client_from_args(args)
    offset = args.offset or 0
    total_written = 0
    first_call = True

    try:
        output_fh = open(args.output, "w", newline="", encoding="utf-8")
    except OSError as exc:
        return print_error(f"could not write {args.output}: {exc}")

    try:
        with output_fh as fh:
            writer = csv.writer(fh)
            writer.writerow(_DISCOVER_RESOURCES_CSV_COLUMNS)

            while True:
                if not first_call:
                    time.sleep(_DISCOVER_RESOURCES_CSV2_RATE_SECONDS)
                first_call = False

                response = client.discovery_resources(type_=args.type, limit=args.limit, offset=offset)
                body = response_body(response)
                print(".", end="", flush=True)

                if not response.ok:
                    print()
                    return print_error(
                        f"CDP /discovery/resources returned HTTP {response.status_code} "
                        f"at offset={offset}: {body}"
                    )

                items = body.get("items", []) if isinstance(body, dict) else []
                for item in items:
                    description = item.get("description", "")
                    resource = item.get("resource", "")
                    quality = item.get("quality") or {}
                    l30_days_total_calls = quality.get("l30DaysTotalCalls", "")
                    l30_days_unique_payers = quality.get("l30DaysUniquePayers", "")
                    for accept in item.get("accepts", []):
                        extra = accept.get("extra") or {}
                        writer.writerow(
                            [
                                accept.get("amount", ""),
                                accept.get("asset", ""),
                                accept.get("network", ""),
                                accept.get("payTo", ""),
                                l30_days_total_calls,
                                l30_days_unique_payers,
                                description,
                                resource,
                                extra.get("receiverAuthorizer", ""),
                                accept.get("scheme", ""),
                            ]
                        )
                fh.flush()

                returned = len(items)
                total_written += returned
                offset += returned

                pagination = body.get("pagination") if isinstance(body, dict) else None
                total = pagination.get("total") if isinstance(pagination, dict) else None

                if returned == 0 or (total is not None and offset >= total):
                    break
    finally:
        print()  # end the line of "." progress dots

    print(f"wrote {total_written} resource(s) to {args.output}")
    return 0


def cmd_cdp_discover_merchant(args: argparse.Namespace) -> int:
    client = _cdp_client_from_args(args)
    response = client.discovery_merchant(args.pay_to, limit=args.limit, offset=args.offset)
    return print_response(response, args.json, label="CDP /discovery/merchant")


_DISCOVER_MERCHANT_CSV_COLUMNS = (
    "payTo",
    "serviceName",
    "amount",
    "asset",
    "network",
    "resource",
    "l30DaysTotalCalls",
    "l30DaysUniquePayers",
    "description",
    "tags",
)

_DISCOVER_MERCHANT_CSV_PAUSE_SECONDS = 1.0


def cmd_cdp_discover_merchant_csv(args: argparse.Namespace) -> int:
    client = _cdp_client_from_args(args)
    pay_tos = [p.strip() for p in args.pay_to.split(",") if p.strip()]

    writer = csv.writer(sys.stdout)
    writer.writerow(_DISCOVER_MERCHANT_CSV_COLUMNS)

    had_error = False
    for i, pay_to in enumerate(pay_tos):
        if i > 0:
            time.sleep(_DISCOVER_MERCHANT_CSV_PAUSE_SECONDS)

        response = client.discovery_merchant(pay_to, limit=args.limit, offset=args.offset)
        body = response_body(response)
        if not response.ok:
            had_error = True
            print_error(f"CDP /discovery/merchant returned HTTP {response.status_code} for payTo={pay_to}: {body}")
            continue

        resources = body.get("resources", []) if isinstance(body, dict) else []
        for resource in resources:
            description = resource.get("description", "")
            service_name = resource.get("serviceName", "")
            resource_url = resource.get("resource", "")
            quality = resource.get("quality") or {}
            l30_days_total_calls = quality.get("l30DaysTotalCalls", "")
            l30_days_unique_payers = quality.get("l30DaysUniquePayers", "")
            tags = ", ".join(resource.get("tags") or [])
            for accept in resource.get("accepts", []):
                writer.writerow(
                    [
                        accept.get("payTo", pay_to),
                        service_name,
                        accept.get("amount", ""),
                        accept.get("asset", ""),
                        accept.get("network", ""),
                        resource_url,
                        l30_days_total_calls,
                        l30_days_unique_payers,
                        description,
                        tags,
                    ]
                )
    return 1 if had_error else 0


def cmd_cdp_search(args: argparse.Namespace) -> int:
    client = _cdp_client_from_args(args)
    response = client.discovery_search(
        query=args.query,
        network=args.network,
        asset=args.asset,
        scheme=args.scheme,
        payTo=args.pay_to,
        urlSubstring=args.url_substring,
        maxUsdPrice=args.max_usd_price,
        extensions=args.extensions or None,
        tags=args.tags or None,
        bundleSlugs=args.bundle_slugs or None,
        curatedOnly=args.curated_only or None,
        limit=args.limit,
    )
    return print_response(response, args.json, label="CDP /discovery/search")


def cmd_cdp_bundles(args: argparse.Namespace) -> int:
    client = _cdp_client_from_args(args)
    response = client.discovery_bundles()
    return print_response(response, args.json, label="CDP /discovery/bundles")


def cmd_cdp_bundle(args: argparse.Namespace) -> int:
    client = _cdp_client_from_args(args)
    response = client.discovery_bundle(args.slug)
    return print_response(response, args.json, label=f"CDP /discovery/bundles/{args.slug}")


def cmd_cdp_mcp(args: argparse.Namespace) -> int:
    client = _cdp_client_from_args(args)
    params = json.loads(args.params) if args.params else None
    response = client.discovery_mcp(args.method, params=params, request_id=args.id)
    return print_response(response, args.json, label="CDP /discovery/mcp")


def cmd_cdp_validate(args: argparse.Namespace) -> int:
    client = _cdp_client_from_args(args)
    response = client.validate(args.resource, method=args.method)
    return print_response(response, args.json, label="CDP /validate")


# --------------------------------------------------------------------------
# Argument parser
# --------------------------------------------------------------------------


def _print_facilitator_analysis(result: dict[str, Any]) -> None:
    print(f"\n=== {result['id']}: {result['name']} ===")
    for host in result["hosts"]:
        print(f"  {host['hostname']}  ({host.get('role', '?')})")
        if host.get("error"):
            print(f"    error        : {host['error']}")
            continue

        ip = host.get("ip")
        print(f"    ip           : {ip or 'could not resolve'}")
        if host.get("reverse_dns"):
            print(f"    reverse dns  : {host['reverse_dns']}")

        geo = host.get("geo") or {}
        if geo.get("status") == "success":
            loc = ", ".join(p for p in (geo.get("city"), geo.get("regionName"), geo.get("country")) if p)
            print(f"    ip geo       : {loc or 'unknown'}")
            org_bits = ", ".join(p for p in (geo.get("isp"), geo.get("org"), geo.get("as")) if p)
            if org_bits:
                print(f"    ip org/isp   : {org_bits}")
            if geo.get("hosting"):
                print("    ip hosting   : yes (datacenter/hosting IP)")
        elif geo:
            print(f"    ip geo       : lookup failed ({geo.get('message', 'unknown error')})")

        ssl_info = host.get("ssl") or {}
        if ssl_info.get("valid"):
            names = ", ".join(
                f"{k}={v}" for k, v in (("CN", ssl_info.get("subject_cn")), ("O", ssl_info.get("subject_o"))) if v
            )
            print(f"    ssl subject  : {names or 'n/a'}")
            locs = ", ".join(
                f"{k}={v}"
                for k, v in (
                    ("L", ssl_info.get("subject_locality")),
                    ("ST", ssl_info.get("subject_state")),
                    ("C", ssl_info.get("subject_country")),
                )
                if v
            )
            print(f"    ssl location : {locs or 'not present (typical for a DV certificate)'}")
            print(
                f"    ssl issuer   : {ssl_info.get('issuer_o') or ssl_info.get('issuer_cn') or 'n/a'}"
                f"  (expires in {ssl_info.get('days_until_expiry', '?')} days)"
            )
        else:
            print(f"    ssl          : {ssl_info.get('error', 'unavailable')}")

    for domain, w in result["whois"].items():
        print(f"  whois ({domain}):")
        if w.get("error"):
            print(f"    error        : {w['error']}")
            continue
        print(f"    registrar    : {w.get('registrar_name') or 'n/a'}")
        print(f"    registrant   : {w.get('registrant_name') or 'n/a (often redacted)'}")
        print(f"    country      : {w.get('registrant_country') or 'n/a'}")
        age = w.get("age_days")
        print(f"    domain age   : {f'{age} days' if age is not None else 'n/a'}")


def _host_geo_country(host: dict[str, Any]) -> Optional[str]:
    geo = host.get("geo") or {}
    if geo.get("status") == "success":
        return geo.get("country")
    return None


def _summary_row(result: dict[str, Any]) -> tuple[str, Optional[str], Optional[str]]:
    api_country = docs_country = None
    for host in result["hosts"]:
        if host.get("role") == "api":
            api_country = _host_geo_country(host)
        elif host.get("role") == "docs":
            docs_country = _host_geo_country(host)
    # base_url and docs_url often share one hostname (deduped to a single
    # "api"-role host); in that case the docs country is the api country.
    if docs_country is None and len(result["hosts"]) == 1:
        docs_country = api_country
    return result["name"], api_country, docs_country


def cmd_analyse_facilitators(args: argparse.Namespace) -> int:
    if args.defunct:
        entries = registry.DEFUNCT_FACILITATORS
    else:
        entries = registry.FACILITATORS
    if args.id:
        unknown = [fid for fid in args.id if fid not in entries]
        if unknown:
            return print_error(f"unknown facilitator id(s): {', '.join(unknown)}")
        entries = {fid: entries[fid] for fid in args.id}

    results = net_analysis.analyse_facilitators(entries, timeout=args.probe_timeout, max_workers=args.workers)

    if args.summary:
        rows = [_summary_row(r) for r in results]
        if args.json:
            print(
                json.dumps(
                    [
                        {"name": name, "api_country": api, "docs_country": docs}
                        for name, api, docs in rows
                    ],
                    indent=2,
                )
            )
            return 0
        print_table(
            ["FACILITATOR", "API URL COUNTRY", "DOCS URL COUNTRY"],
            [(name, api or "unknown", docs or "unknown") for name, api, docs in rows],
        )
        return 0

    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return 0

    for result in results:
        _print_facilitator_analysis(result)
    return 0


def cmd_analyse_domain(args: argparse.Namespace) -> int:
    host = net_analysis.parse_hostname(args.domain) or args.domain
    result = net_analysis.analyse_single_domain(host, timeout=args.probe_timeout)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    _print_facilitator_analysis(result)
    return 0


def cmd_analyse_domains(args: argparse.Namespace) -> int:
    try:
        with open(args.file, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
    except OSError as exc:
        return print_error(f"could not read {args.file}: {exc}")

    urls: list[str] = []
    for row in rows:
        urls.append(row[args.column] if len(row) > args.column else "")

    hostnames = [net_analysis.parse_hostname(url) for url in urls]
    unique_hosts = sorted({h for h in hostnames if h})

    ip_by_host: dict[str, Optional[str]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(net_analysis.resolve_ip, host): host for host in unique_hosts}
        for future in as_completed(futures):
            ip_by_host[futures[future]] = future.result()

    ips = [ip for ip in ip_by_host.values() if ip]
    geo_by_ip = net_analysis.geolocate_batch(ips)

    writer = csv.writer(sys.stdout)
    writer.writerow(["url", "country"])
    for url, host in zip(urls, hostnames):
        ip = ip_by_host.get(host) if host else None
        geo = geo_by_ip.get(ip) if ip else None
        country = geo.get("country") if geo and geo.get("status") == "success" else ""
        writer.writerow([url, country])
    return 0


def _fetch_supported_addresses(
    fid: str, entry: dict[str, Any], timeout: float
) -> tuple[str, list[tuple[str, str]]]:
    """Best-effort /supported fetch, merged with any registry known_addresses.

    Returns (fid, [(address, source), ...]) where source is "D" (pulled live
    from /supported) or "S" (a static fallback from the registry's
    known_addresses, only used for an address /supported didn't also report).

    Failures fetching /supported (network error, auth required, bad JSON)
    just mean no *dynamic* addresses were recoverable - not a hard error for
    the whole command - so they fall back to known_addresses alone.
    """
    dynamic: list[str] = []
    try:
        if entry["api"] == "cdp":
            client = CdpClient(
                key_id=os.environ.get("CDP_API_KEY_ID"),
                key_secret=os.environ.get("CDP_API_KEY_SECRET"),
                timeout=timeout,
            )
        else:
            base_url, headers = registry.resolve_generic_target(entry)
            client = GenericFacilitatorClient(base_url, headers=headers, timeout=timeout)
        response = client.supported()
        if response.ok:
            dynamic = blockchain_addresses.extract_addresses(response.json())
    except Exception:  # noqa: BLE001 - this is a best-effort sweep
        pass

    combined: dict[str, tuple[str, str]] = {addr.lower(): (addr, "D") for addr in dynamic}
    for addr in entry.get("known_addresses") or []:
        combined.setdefault(addr.lower(), (addr, "S"))

    return fid, sorted(combined.values(), key=lambda pair: pair[0].lower())


def cmd_analyse_facilitators_bc(args: argparse.Namespace) -> int:
    print("analysis started")

    entries = registry.DEFUNCT_FACILITATORS if args.defunct else registry.FACILITATORS
    if args.id:
        unknown = [fid for fid in args.id if fid not in entries]
        if unknown:
            return print_error(f"unknown facilitator id(s): {', '.join(unknown)}")
        entries = {fid: entries[fid] for fid in args.id}

    addresses_by_fid: dict[str, list[tuple[str, str]]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_fetch_supported_addresses, fid, entry, args.probe_timeout): fid
            for fid, entry in entries.items()
        }
        for future in as_completed(futures):
            fid, addrs = future.result()
            addresses_by_fid[fid] = addrs

    threatdb = cloudbric_threatdb.CloudbricThreatDbClient(
        timeout=args.probe_timeout, delay=args.lookup_delay
    )

    # Dedupe lookups (the same address can recur across facilitators), and
    # query them one at a time - this endpoint is unauthenticated and
    # explicitly rate-limited, so no concurrency here.
    unique_addresses = sorted({addr for addrs in addresses_by_fid.values() for addr, _src in addrs})
    info_by_address: dict[str, str] = {}
    for addr in unique_addresses:
        try:
            info_by_address[addr] = threatdb.describe(addr)
        except Exception as exc:  # noqa: BLE001
            info_by_address[addr] = f"Cloudbric ThreatDB lookup failed: {exc}"

    rows: list[tuple[str, str, str, str]] = []
    for fid in sorted(entries, key=lambda i: entries[i]["name"].lower()):
        name = entries[fid]["name"]
        addrs = sorted(addresses_by_fid.get(fid, []), key=lambda pair: pair[0].lower())
        if not addrs:
            rows.append((name, "", "none available", ""))
            continue
        for addr, source in addrs:
            rows.append((name, source, addr, info_by_address.get(addr, "")))

    if args.json:
        print(
            json.dumps(
                [
                    {"name": n, "source": s, "address": a, "cloudbric_threatdb": i}
                    for n, s, a, i in rows
                ],
                indent=2,
            )
        )
        return 0

    print_table(["FACILITATOR", "S/D", "ADDRESS", "CLOUDBRIC THREATDB INFO"], rows)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="x402tool", description="Analyze and exercise x402 payment protocol facilitators."
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    parser.add_argument("-v", "--verbose", action="store_true", help="print outgoing requests")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser(
        "list-facilitators", help="list known operational x402 facilitators"
    )
    p_list.add_argument("--access", choices=("public", "gated", "gated_paid"))
    p_list.add_argument("--network", help="filter by substring match against a network name")
    _add_common_output_args(p_list)
    p_list.set_defaults(func=cmd_list_facilitators)

    p_list_defunct = sub.add_parser(
        "list-defunct-facil",
        help="list known x402 facilitators that are no longer operational",
    )
    p_list_defunct.add_argument("--access", choices=("public", "gated", "gated_paid"))
    p_list_defunct.add_argument("--network", help="filter by substring match against a network name")
    _add_common_output_args(p_list_defunct)
    p_list_defunct.set_defaults(func=cmd_list_defunct_facilitators)

    p_list_domains = sub.add_parser(
        "list-fac-domains",
        help="output every operational facilitator's API/doc domains as CSV",
    )
    p_list_domains.set_defaults(func=cmd_list_fac_domains)

    p_show = sub.add_parser("show", help="show details for one known facilitator")
    p_show.add_argument("facilitator", help="facilitator id, see `list-facilitators`")
    _add_common_output_args(p_show)
    p_show.set_defaults(func=cmd_show)

    p_request = sub.add_parser(
        "request",
        help="request a URL and show the x402 402 Payment Required details it returns",
    )
    p_request.add_argument("url", help="URL of the x402-gated resource to request")
    p_request.add_argument("--method", default="GET", choices=("GET", "POST"))
    p_request.add_argument(
        "--header",
        action="append",
        default=[],
        type=_parse_header,
        metavar="'Name: Value'",
        help="extra HTTP header to send; may be repeated",
    )
    _add_common_output_args(p_request)
    p_request.set_defaults(func=cmd_request)

    p_supported = sub.add_parser(
        "supported", help="GET /supported (or CDP's /v2/x402/supported) for a facilitator"
    )
    p_supported.add_argument(
        "facilitator", help="facilitator id (see `list-facilitators`) or a raw base URL"
    )
    _add_generic_auth_args(p_supported)
    _add_cdp_auth_args(p_supported)
    _add_common_output_args(p_supported)
    p_supported.set_defaults(func=cmd_supported)

    p_verify = sub.add_parser(
        "verify",
        help="POST /verify (or CDP's /v2/x402/verify) with a pre-built, already-signed payment",
    )
    p_verify.add_argument(
        "facilitator", help="facilitator id (see `list-facilitators`) or a raw base URL"
    )
    _add_generic_auth_args(p_verify)
    _add_cdp_auth_args(p_verify)
    _add_payment_body_args(p_verify)
    _add_common_output_args(p_verify)
    p_verify.set_defaults(func=cmd_verify)

    p_settle = sub.add_parser(
        "settle",
        help="POST /settle (or CDP's /v2/x402/settle) with a pre-built, already-signed payment",
    )
    p_settle.add_argument(
        "facilitator", help="facilitator id (see `list-facilitators`) or a raw base URL"
    )
    _add_generic_auth_args(p_settle)
    _add_cdp_auth_args(p_settle)
    _add_payment_body_args(p_settle)
    _add_common_output_args(p_settle)
    p_settle.set_defaults(func=cmd_settle)

    p_cdp = sub.add_parser(
        "cdp", help="Coinbase CDP-only extensions: bazaar discovery, MCP, endpoint validation"
    )
    cdp_sub = p_cdp.add_subparsers(dest="cdp_command", required=True)

    p_dr = cdp_sub.add_parser("discover-resources", help="list active discovered x402 resources")
    p_dr.add_argument("--type", help='protocol type filter, e.g. "http"')
    p_dr.add_argument("--limit", type=int)
    p_dr.add_argument("--offset", type=int)
    _add_cdp_auth_args(p_dr)
    _add_common_output_args(p_dr)
    p_dr.set_defaults(func=cmd_cdp_discover_resources)

    p_dr_csv = cdp_sub.add_parser(
        "discover-resources-csv",
        help="list active discovered x402 resources as CSV (one row per accepted payment option)",
    )
    p_dr_csv.add_argument("--type", help='protocol type filter, e.g. "http"')
    p_dr_csv.add_argument("--limit", type=int)
    p_dr_csv.add_argument("--offset", type=int)
    _add_cdp_auth_args(p_dr_csv)
    p_dr_csv.set_defaults(func=cmd_cdp_discover_resources_csv)

    p_dr_csv2 = cdp_sub.add_parser(
        "discover-resources-csv2",
        help="like discover-resources-csv, but pages through every resource "
        "(rate-limited to 1 request/sec) and writes them all to a file",
    )
    p_dr_csv2.add_argument("output", help="path to write the CSV output to")
    p_dr_csv2.add_argument("--type", help='protocol type filter, e.g. "http"')
    p_dr_csv2.add_argument("--limit", type=int, help="page size requested per call")
    p_dr_csv2.add_argument("--offset", type=int, help="starting offset (default: 0)")
    _add_cdp_auth_args(p_dr_csv2)
    p_dr_csv2.set_defaults(func=cmd_cdp_discover_resources_csv2)

    p_dm = cdp_sub.add_parser("discover-merchant", help="list a merchant's discovered x402 resources")
    p_dm.add_argument("--pay-to", required=True, help="merchant payment address")
    p_dm.add_argument("--limit", type=int)
    p_dm.add_argument("--offset", type=int)
    _add_cdp_auth_args(p_dm)
    _add_common_output_args(p_dm)
    p_dm.set_defaults(func=cmd_cdp_discover_merchant)

    p_dm_csv = cdp_sub.add_parser(
        "discover-merchant-csv",
        help="list merchants' discovered x402 resources as CSV (one row per accepted payment option)",
    )
    p_dm_csv.add_argument(
        "--pay-to",
        required=True,
        help="merchant payment address, or a comma-separated list to query one at a time",
    )
    p_dm_csv.add_argument("--limit", type=int)
    p_dm_csv.add_argument("--offset", type=int)
    _add_cdp_auth_args(p_dm_csv)
    p_dm_csv.set_defaults(func=cmd_cdp_discover_merchant_csv)

    p_search = cdp_sub.add_parser("search", help="search active discovered x402 resources")
    p_search.add_argument("--query", help="full-text/semantic search query")
    p_search.add_argument("--network", help="CAIP-2 or legacy network name filter")
    p_search.add_argument("--asset", help="asset address filter")
    p_search.add_argument("--scheme", help='payment scheme filter, e.g. "exact"')
    p_search.add_argument("--pay-to", help="merchant payment address filter")
    p_search.add_argument("--url-substring", help="case-insensitive substring match against resource URL")
    p_search.add_argument("--max-usd-price", help="max USD price filter")
    p_search.add_argument("--extensions", action="append", default=[], help="protocol extension filter; repeatable")
    p_search.add_argument("--tags", action="append", default=[], help="provider tag filter; repeatable")
    p_search.add_argument("--bundle-slugs", action="append", default=[], help="curated bundle slug filter; repeatable")
    p_search.add_argument("--curated-only", action="store_true", help="restrict to Coinbase-curated resources")
    p_search.add_argument("--limit", type=int, help="max results, 1-20 (default 20)")
    _add_cdp_auth_args(p_search)
    _add_common_output_args(p_search)
    p_search.set_defaults(func=cmd_cdp_search)

    p_bundles = cdp_sub.add_parser("bundles", help="list curated x402 workflow bundles")
    _add_cdp_auth_args(p_bundles)
    _add_common_output_args(p_bundles)
    p_bundles.set_defaults(func=cmd_cdp_bundles)

    p_bundle = cdp_sub.add_parser("bundle", help="get a single curated x402 workflow bundle")
    p_bundle.add_argument("slug", help="bundle slug, see `cdp bundles`")
    _add_cdp_auth_args(p_bundle)
    _add_common_output_args(p_bundle)
    p_bundle.set_defaults(func=cmd_cdp_bundle)

    p_mcp = cdp_sub.add_parser("mcp", help="send a JSON-RPC MCP request to the discovery endpoint")
    p_mcp.add_argument("--method", required=True, help='MCP method, e.g. "tools/list"')
    p_mcp.add_argument("--params", help="JSON object of method params")
    p_mcp.add_argument("--id", default=1, help="JSON-RPC request id")
    _add_cdp_auth_args(p_mcp)
    _add_common_output_args(p_mcp)
    p_mcp.set_defaults(func=cmd_cdp_mcp)

    p_validate = cdp_sub.add_parser(
        "validate", help="probe a seller's endpoint live for bazaar-discovery readiness"
    )
    p_validate.add_argument("--resource", required=True, help="HTTPS URL of the x402 endpoint to validate")
    p_validate.add_argument("--method", default="GET", choices=("GET", "POST"))
    _add_cdp_auth_args(p_validate)
    _add_common_output_args(p_validate)
    p_validate.set_defaults(func=cmd_cdp_validate)

    p_analyse = sub.add_parser(
        "analyse-facilitators",
        help="passively geolocate/fingerprint facilitator hosts: IP geo, SSL cert subject, WHOIS",
    )
    p_analyse.add_argument(
        "--id",
        action="append",
        default=[],
        help="only analyse this facilitator id; may be repeated (default: all)",
    )
    p_analyse.add_argument(
        "--defunct",
        action="store_true",
        help="analyse the defunct facilitator list instead of the operational one",
    )
    p_analyse.add_argument(
        "--workers", type=int, default=10, help="parallel network probes (default: 10)"
    )
    p_analyse.add_argument(
        "--probe-timeout",
        type=float,
        default=8.0,
        help="per-host timeout in seconds for DNS/SSL probes (default: 8)",
    )
    p_analyse.add_argument(
        "--summary",
        action="store_true",
        help="print just a table of facilitator name, API URL geolocated country, "
        "and docs URL geolocated country",
    )
    _add_common_output_args(p_analyse)
    p_analyse.set_defaults(func=cmd_analyse_facilitators)

    p_adom = sub.add_parser(
        "analyse-domain",
        help="run the same IP-geo/SSL-subject/WHOIS analysis as analyse-facilitators "
        "against one arbitrary domain",
    )
    p_adom.add_argument("domain", help="domain or URL to analyse")
    p_adom.add_argument(
        "--probe-timeout",
        type=float,
        default=8.0,
        help="timeout in seconds for DNS/SSL/WHOIS probes (default: 8)",
    )
    _add_common_output_args(p_adom)
    p_adom.set_defaults(func=cmd_analyse_domain)

    p_adoms = sub.add_parser(
        "analyse-domains",
        help="geolocate the IPs behind URLs in one column of a CSV file",
    )
    p_adoms.add_argument("file", help="path to a CSV file")
    p_adoms.add_argument("column", type=int, help="0-indexed column number containing URLs")
    p_adoms.add_argument(
        "--workers", type=int, default=10, help="parallel DNS lookups (default: 10)"
    )
    p_adoms.set_defaults(func=cmd_analyse_domains)

    p_bc = sub.add_parser(
        "analyse-facilitators-bc",
        help="gather each facilitator's blockchain addresses from /supported and "
        "screen them via Cloudbric's Hacker Wallet threat DB",
    )
    p_bc.add_argument(
        "--id",
        action="append",
        default=[],
        help="only analyse this facilitator id; may be repeated (default: all)",
    )
    p_bc.add_argument(
        "--defunct",
        action="store_true",
        help="analyse the defunct facilitator list instead of the operational one",
    )
    p_bc.add_argument(
        "--workers",
        type=int,
        default=10,
        help="parallel /supported requests across facilitators (default: 10); "
        "Cloudbric ThreatDB lookups always run one at a time regardless",
    )
    p_bc.add_argument(
        "--probe-timeout",
        type=float,
        default=15.0,
        help="per-request timeout in seconds (default: 15)",
    )
    p_bc.add_argument(
        "--lookup-delay",
        type=float,
        default=1.0,
        help="seconds to wait before each Cloudbric ThreatDB lookup, since that "
        "endpoint is unauthenticated and rate-limited (default: 1.0)",
    )
    _add_common_output_args(p_bc)
    p_bc.set_defaults(func=cmd_analyse_facilitators_bc)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CdpClientError as exc:
        return print_error(str(exc))
    except requests.exceptions.RequestException as exc:
        return print_error(f"request failed: {exc}")
