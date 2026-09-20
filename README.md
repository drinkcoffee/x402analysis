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

### API keys

Copy `.env.example` to `.env` and fill in whichever keys the commands you
use need (`CDP_API_KEY_ID`/`CDP_API_KEY_SECRET`, `ETHERSCAN_API_KEY`,
`X402_API_KEY`) - `x402tool` loads `.env` automatically from the current
directory (or a parent of it), no extra setup needed. `.env` is gitignored;
never commit your real keys. An explicit `--api-key`/`--api-key-id` flag, or
a real environment variable already set in your shell, always takes
precedence over `.env`.

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

## Tracing an address's funding source

`funded-by-etherscan <blockchain> <address>` calls Etherscan's unified V2
["Get Address Funded By"](https://docs.etherscan.io/api-reference/endpoint/fundedby)
API to trace an EOA's original funding source: the address, transaction,
block, and timestamp that first sent it value. It's a PRO-tier Etherscan
endpoint (Standard plan and above) and only works for EOAs, not contract
addresses. Requires an Etherscan API key - pass `--api-key` or set
`$ETHERSCAN_API_KEY`.

`<blockchain>` accepts a chain name (`base`, `polygon`, `ethereum`, ...), a
bare numeric chain id, or an `eip155:<id>` CAIP-2 string - Etherscan's API
only covers EVM chains, so non-EVM ones (Solana, XRPL, NEAR, Stellar,
Canton, ...) aren't supported and are rejected up front with a clear error.

```bash
export ETHERSCAN_API_KEY=...
x402tool funded-by-etherscan base 0x742d35Cc6634C0532925a3b844Bc454e4438f44e
x402tool funded-by-etherscan eip155:1 0x742d35Cc6634C0532925a3b844Bc454e4438f44e --json
```

## Listing an address's transactions

`assoc-txs-blockscout <blockchain> <address>` calls
[Blockscout's Pro API](https://www.blog.blockscout.com/blockscout-pro-api-postman/)
to list transactions to or from an address, one unified endpoint across
every Blockscout-indexed EVM chain. `<blockchain>` takes the same chain
name/numeric-id/`eip155:<id>` forms as `funded-by-etherscan`. Requires a
Blockscout API key - pass `--api-key` or set `$BLOCKSCOUT_API_KEY` (free
tier: 100K credits/day, 5 req/s, no card required, from
[dev.blockscout.com](https://dev.blockscout.com/)).

Notably, this specific endpoint is itself x402-gated: hit it with no
recognized key and Blockscout responds `402` with its own x402
payment-required envelope, offering to accept an on-chain USDC payment as
an alternative to an API key. This client only ever takes the API-key
route.

Pagination is 50 transactions per page; `--pages N` (default 1) fetches N
pages, or `--all-pages` fetches every page the API has.

```bash
export BLOCKSCOUT_API_KEY=...
x402tool assoc-txs-blockscout base 0x742d35Cc6634C0532925a3b844Bc454e4438f44e
x402tool assoc-txs-blockscout ethereum 0x742d35Cc6634C0532925a3b844Bc454e4438f44e --pages 3 --json
```

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
`same` placeholder) just gets a blank country rather than an error. After
those rows, it appends a blank line followed by a `country,count` summary -
one row per country, sorted by count descending, counting each unique host
once (so duplicate/blank/invalid rows don't skew it) - so a domain list also
tells you at a glance where those servers are hosted.

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

`list-fac-bc` is the raw address data behind `analyse-facilitators-bc`,
without the ThreatDB lookups: `facilitator,source,blockchain,address` CSV,
one row per (address, network) pair - so an address reused across networks
(e.g. the same EVM address for both Base and Polygon) gets one row per
network rather than being collapsed. `source` is `S`/`D` as above.

```bash
x402tool list-fac-bc
x402tool list-fac-bc --id payai --id celo > addresses.csv
```

`check-sanctions <file.csv> <column> --output out.csv` screens addresses
from any CSV file (not just this tool's own output) against
[ChainQuery's public Sanctions Check API](https://chainquery.com/products/sanctions-api)
(`GET /api/sanctions/check/<address>`, unauthenticated, no key). ChainQuery
describes this as an educational research tool aggregating OFAC/UK
OFSI/OpenSanctions feeds - explicitly not a compliance product. It writes
`address,status` CSV (`clear`, `sanctioned (N sources)`, `invalid address`,
or `error: ...`), printing a `.` per API call for progress; a repeated
address only gets looked up once, and a blank cell is reported as `no
address` without a network call. Calls are paced one at a time,
`--rate-delay` seconds apart (default 1.0) - note that's far more
aggressive than ChainQuery's own published limit of 30 requests/hour/IP, so
expect HTTP 429s on any nontrivial list; a 429 waits out the server's
`Retry-After` once and retries before giving up on that address.

```bash
x402tool check-sanctions addresses.csv 1 --output results.csv
x402tool check-sanctions addresses.csv 0 --output results.csv --rate-delay 5.0
```

## Scraping x402scan.com's server directory

`scrape-servers` scrapes [x402scan.com](https://www.x402scan.com/)'s server/service
directory, which has no documented public API. x402scan is a Next.js app;
its server list and per-server resource data are embedded directly in each
page's HTML as React Server Component payload, so `x402scan_scraper.py`
fetches the plain HTML (no browser needed) and parses that embedded JSON
rather than screen-scraping rendered markup. This is a third-party site's
internal implementation detail, not a stable contract - it can break if
x402scan changes their data model.

For each server it reports: name/description, an inferred API URL, a doc
URL (x402scan groups multiple domains - e.g. an `api.`/`x402.` subdomain
plus a bare marketing domain - under one server; the non-API one is
reported as the doc link, or it's the same URL when there's only one), the
full list of resources/endpoints (URL, method, price, description, tags),
and every settlement address referenced across those resources.

```bash
x402tool scrape-servers                      # print JSON for every server to stdout
x402tool scrape-servers servers.json         # write it to a file instead
x402tool scrape-servers --limit 10           # just the first 10, for a quick look
x402tool scrape-servers servers.json --workers 8 --delay 0.2
```

A full run visits x402scan's homepage once plus one page per server
(~350+ as of this writing), so it takes a few minutes; `--workers` controls
how many of those run concurrently and `--delay` adds a pause before each
one if you want to be gentler on their site.

`extract-domains <file.json>` pulls every unique domain referenced by a URL
in any JSON file - `scrape-servers` output or otherwise - and prints them
one per line, alphabetically (e.g. `https://api.example.com/foo` and
`https://api.example.com/bar` both collapse to `api.example.com`; a
subdomain like `sub.example.com` stays distinct from `example.com`). It
walks the whole JSON structure (objects, arrays, and URLs embedded inside
longer text values, e.g. a description that mentions one) rather than
looking at specific known fields, so it works on arbitrary JSON, not just
this tool's own output.

```bash
x402tool scrape-servers servers.json && x402tool extract-domains servers.json
x402tool extract-domains some-other-file.json
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
  chainquery_client.py  ChainQuery Sanctions API client for check-sanctions
  etherscan_client.py   Etherscan V2 "fundedby" client for funded-by-etherscan
  blockscout_client.py  Blockscout Pro API client for assoc-txs-blockscout
  x402scan_scraper.py   x402scan.com server-directory scraper for scrape-servers
  formatting.py         table/JSON output helpers
  cli.py                argparse wiring
```
