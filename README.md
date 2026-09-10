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
x402tool list-facilitators                 # table of all known facilitators
x402tool list-facilitators --access public # filter by access type
x402tool list-facilitators --network solana
x402tool show coinbase-cdp                 # full detail for one facilitator
```

The registry is compiled from the community-maintained
[Swader/x402facilitators](https://github.com/Swader/x402facilitators) list and
Coinbase's CDP API reference, as a snapshot — facilitator infrastructure moves
fast, so treat entries as a starting point and confirm with `supported`.

Two Coinbase entries are listed separately:

- `coinbase` - the free, unauthenticated public facilitator
  (`facilitator.cdp.coinbase.com`), speaking the plain x402 facilitator
  contract (`/verify`, `/settle`, `/supported`).
- `coinbase-cdp` - Coinbase's authenticated CDP Platform API
  (`api.cdp.coinbase.com/platform/v2/x402/*`), which additionally exposes
  bazaar discovery, MCP, and endpoint-validation routes. Requires a CDP API
  key pair.

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
x402tool cdp discover-merchant --pay-to 0x...
x402tool cdp search --query "weather" [--network eip155:8453] [--scheme exact] ...
x402tool cdp bundles
x402tool cdp bundle <slug>
x402tool cdp mcp --method tools/list [--params '{"...": "..."}']
x402tool cdp validate --resource https://api.example.com/some/endpoint
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

## Layout

```
x402tool/
  registry.py         known facilitators + auth metadata
  cdp_auth.py          CDP bearer JWT signing
  cdp_client.py        client for api.cdp.coinbase.com/platform/v2/x402/*
  generic_client.py     client for the plain /verify, /settle, /supported contract
  formatting.py         table/JSON output helpers
  cli.py                argparse wiring
```
