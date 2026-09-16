# x402analysis

`x402tool` is a command-line tool for analyzing the [x402](https://www.x402.org/)
payment protocol ecosystem: it maintains a registry of known facilitators and
can exercise their HTTP APIs (verify, settle, supported, and Coinbase's
bazaar-discovery extensions).

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the `x402tool` command (also runnable as `python -m x402tool`).

## Facilitator registry

```bash
x402tool list-facilitators                 # table of known, operational facilitators
x402tool list-facilitators --access public # filter by access type
x402tool list-facilitators --network solana
x402tool list-defunct-facil                # facilitators no longer reachable/maintained
x402tool show coinbase-cdp                 # full detail for one facilitator
x402tool list-fac-domains                  # CSV of every facilitator's API/doc domains
```

`list-fac-domains` prints `name,type,url` for every operational facilitator
- one `API` row (its `base_url`) and one `doc` row (its `docs_url`, or the
literal word `same` when the two URLs are identical).

The registry is compiled from the community-maintained
[Swader/x402facilitators](https://github.com/Swader/x402facilitators) list and
Coinbase's CDP API reference, as a snapshot — facilitator infrastructure moves
fast, so treat entries as a starting point and confirm with `supported`.

Coinbase has two entries: `coinbase-cdp`, the operational, authenticated CDP
Platform API (`api.cdp.coinbase.com/platform/v2/x402/*`), which also exposes
bazaar discovery, MCP, and endpoint-validation routes and requires a CDP API
key pair; and `coinbase`, the free, unauthenticated public facilitator that
previously ran at `facilitator.cdp.coinbase.com` speaking the plain x402
facilitator contract (`/verify`, `/settle`, `/supported`) - its host no longer
resolves, so it now lives in `list-defunct-facil` rather than
`list-facilitators`.

## Checking an x402-gated resource

`request` hits a resource server URL directly (not a facilitator) and shows
the x402 payment-required details it returns on its `402` response: protocol
version, the resource's own description, and every accepted payment option
(scheme, network, amount, asset, `payTo`, timeout, and any scheme-specific
`extra`).

```bash
x402tool request https://api.example.com/some/paid/endpoint
x402tool request https://api.example.com/some/paid/endpoint --json
x402tool request https://api.example.com/some/paid/endpoint --method POST --header 'X-Foo: bar'
```

If the server responds with something other than `402`, it prints a warning
and dumps whatever it did return instead, with a non-zero exit code.

## Exercising the core x402 facilitator API

Every facilitator (generic or CDP) supports the same three verbs:

```bash
x402tool supported <facilitator>
x402tool verify <facilitator> --payload payload.json --requirements requirements.json
x402tool settle <facilitator>  --payload payload.json --requirements requirements.json
```

`<facilitator>` is either a registry id (`x402tool list-facilitators`) or a
raw `https://...` base URL for an ad hoc facilitator not in the registry.

`verify`/`settle` need an already-signed x402 payment payload (EIP-3009/Permit2
signature for EVM, or a signed transaction for Solana) plus matching payment
requirements - this tool does not build or sign payments itself. Supply them
as JSON files with `--payload`/`--requirements`, or as one combined file with
`--body` shaped as `{"x402Version": ..., "paymentPayload": ..., "paymentRequirements": ...}`.
Either flag also accepts `-` to read from stdin.

For gated facilitators, pass credentials with `--api-key` (routed to whatever
header/query scheme that facilitator's registry entry declares - see
`x402tool show <id>`) or `--header 'Name: Value'` (repeatable) for anything
bespoke.

Add `--json` to any command for a machine-readable `{"status": ..., "body": ...}`
envelope instead of the human-readable form, and `-v` to print outgoing
requests.

## Coinbase CDP-only extensions

The CDP Platform API also exposes bazaar discovery, MCP, and endpoint
validation, which aren't part of the core x402 spec and so aren't generic
across facilitators. These live under the `cdp` subcommand and (per CDP's own
API spec) don't require credentials:

```bash
x402tool cdp discover-resources [--type http] [--limit N] [--offset N]
x402tool cdp discover-resources-csv [--type http]       # one CSV row per accepted payment option
x402tool cdp discover-merchant --pay-to 0x...
x402tool cdp discover-merchant-csv --pay-to 0x...
x402tool cdp search --query "weather" [--network eip155:8453] [--scheme exact] ...
x402tool cdp bundles
x402tool cdp bundle <slug>
x402tool cdp mcp --method tools/list [--params '{"...": "..."}']
x402tool cdp validate --resource https://api.example.com/some/endpoint
```

`discover-resources-csv2 <output.csv>` downloads the *entire* discovered-resources
list rather than one page: after each call it counts how many resources came
back and advances the offset by that count, rate-limited to one request per
second and printing a `.` per request so a download that can run for many
minutes (there are tens of thousands of discovered resources) stays visible.
It writes straight to `<output.csv>`, flushing after every page, so an
interrupted run still leaves a valid, readable partial file.

```bash
x402tool cdp discover-resources-csv2 resources.csv
x402tool cdp discover-resources-csv2 resources.csv --type http
```

## CDP authentication

`verify`, `settle`, and `supported` against `coinbase-cdp` require a CDP API
key pair, signed into a short-lived bearer JWT per request (EdDSA for current
Ed25519 keys, ES256 for legacy PEM EC keys):

```bash
export CDP_API_KEY_ID=...
export CDP_API_KEY_SECRET=...
x402tool supported coinbase-cdp
```

or pass `--api-key-id`/`--api-key-secret` directly. Get a key pair from the
[CDP Portal](https://portal.cdp.coinbase.com/).

## Infrastructure analysis

`analyse-facilitators` passively fingerprints the hosts behind each
facilitator's API and docs URLs - no requests are sent to the facilitators'
x402 endpoints themselves. For each host it resolves the IP, geolocates it
(ip-api.com), inspects the TLS certificate's subject/issuer/SAN fields, and
looks up domain WHOIS via RDAP; per facilitator it reports names and
locations derived from the certificate subject, from IP geolocation, and
from WHOIS (registrar/registrant), plus reverse DNS, hosting/datacenter
flags, and certificate expiry. Same methodology as the sibling
`domain-check` tool, but run in parallel across every known facilitator and
batched against ip-api.com's `/batch` endpoint instead of one call per host.

```bash
x402tool analyse-facilitators                  # sweep every operational facilitator
x402tool analyse-facilitators --id celo --id t54
x402tool analyse-facilitators --defunct        # sweep the defunct list instead
x402tool analyse-facilitators --json
x402tool analyse-facilitators --summary        # just a name/API-country/docs-country table
```

`analyse-domain` runs that exact same analysis (IP geo, SSL cert subject,
WHOIS) against one arbitrary domain or URL you give it, rather than a
registry entry - useful for checking a host that isn't (or isn't yet) in the
facilitator registry:

```bash
x402tool analyse-domain facilitator.x402.rs
x402tool analyse-domain https://api.example.com/some/path --json
```

`analyse-domains` does the cheaper half of that (IP geolocation only, no SSL
or WHOIS) in bulk, reading URLs out of one column of a CSV file - handy for
piping in `list-fac-domains` output or any other URL list:

```bash
x402tool analyse-domains urls.csv 2   # column 2 (0-indexed) holds the URLs
x402tool list-fac-domains > domains.csv && x402tool analyse-domains domains.csv 2
```

It prints `url,country` CSV, one row per input row in order; a row whose
column is empty, unresolvable, or not a real URL (e.g. `list-fac-domains`'
`same` placeholder) just gets a blank country rather than an error.

## Blockchain address screening

`analyse-facilitators-bc` pulls each facilitator's `/supported` response,
extracts every blockchain address referenced in it (`signers`, and
`extra.facilitatorAddress` / `receiverAuthorizer` / `feePayer` on individual
scheme entries), merges in any addresses hand-curated in the registry's
`known_addresses` field (for facilitators whose `/supported` doesn't publish
one - either it's gated, or its response is just missing a `signers`/`extra`
block), and screens each one against Cloudbric Labs' Threat DB "Hacker
Wallet" list ([labs.cloudbric.com/threatdb/view#tab3](https://labs.cloudbric.com/threatdb/view#tab3)).
That page has no published API - `cloudbric_threatdb.py` reverse-engineers
the JSON endpoint its own search box calls (`POST
/threatdb/gethackerwalletlist`, a jQuery DataTables backend) and reports each
address's threat level (Low/Medium/High/Very High), report count, and last
activity date, or that it wasn't found in the database. That endpoint is
unauthenticated but explicitly rate-limited, so lookups run one at a time
with a delay between them (`--lookup-delay`, default 1s) and retry with
backoff on failure - never in parallel, unlike the `/supported` fetches
(`--workers`) that gather the addresses in the first place.

Output is a four-column table - facilitator name, `S`/`D` (whether that row's
address is a static `known_addresses` fallback or was pulled live from
`/supported`; an address found both ways counts as `D`), address, Cloudbric
ThreatDB info - sorted alphabetically by facilitator and then ascending by
address; a facilitator with no addresses at all (from either source) gets
one row reading "none available".

```bash
x402tool analyse-facilitators-bc
x402tool analyse-facilitators-bc --id payai --id celo
x402tool analyse-facilitators-bc --lookup-delay 2.0   # even gentler
x402tool analyse-facilitators-bc --json
```

## Layout

```
x402tool/
  registry.py         known facilitators + auth metadata
  cdp_auth.py          CDP bearer JWT signing
  cdp_client.py        client for api.cdp.coinbase.com/platform/v2/x402/*
  generic_client.py     client for the plain /verify, /settle, /supported contract
  net_analysis.py       passive IP/SSL/WHOIS fingerprinting for analyse-facilitators
  blockchain_addresses.py  address extraction from a /supported response
  cloudbric_threatdb.py  Cloudbric Labs Threat DB client for analyse-facilitators-bc
  formatting.py         table/JSON output helpers
  cli.py                argparse wiring
```
