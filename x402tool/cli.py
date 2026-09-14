"""x402tool command-line interface."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from typing import Any, Optional

import requests

from . import registry
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


def cmd_list_facilitators(args: argparse.Namespace) -> int:
    entries = registry.FACILITATORS
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="x402tool", description="Analyze and exercise x402 payment protocol facilitators."
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    parser.add_argument("-v", "--verbose", action="store_true", help="print outgoing requests")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-facilitators", help="list known x402 facilitators")
    p_list.add_argument("--access", choices=("public", "gated", "gated_paid"))
    p_list.add_argument("--network", help="filter by substring match against a network name")
    _add_common_output_args(p_list)
    p_list.set_defaults(func=cmd_list_facilitators)

    p_show = sub.add_parser("show", help="show details for one known facilitator")
    p_show.add_argument("facilitator", help="facilitator id, see `list-facilitators`")
    _add_common_output_args(p_show)
    p_show.set_defaults(func=cmd_show)

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
