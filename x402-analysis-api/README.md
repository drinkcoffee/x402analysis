# x402-analysis-api

A minimal Vercel API server (Python/FastAPI) backed by a Neon Postgres
database, providing programmatic access to x402 ecosystem analysis data.

This file covers running and deploying the server. For the
request/response contract each endpoint exposes, see [API.md](API.md).

## Routes

- `GET /` — unauthenticated. A one-line pointer to `API.md`.
- `GET /status` — unauthenticated. Checks the Neon connection and reports
  whether the server and database are online.
- `GET /favicon.ico` — unauthenticated. The browser-tab icon, generated
  from `api/assets/icon.png`.
- `GET /facilitators` — requires a `read` (or `read_write`) API key.
  Name/risk/active for every facilitator.
- `GET /facilitators/{name}` — requires a `read` (or `read_write`) API key.
  Everything known about one facilitator (matched case-insensitively),
  including its full `uris` rows (IP/geolocation/TLS subject, not just the
  bare URL) and every linked address; 404 if `{name}` matches none.
- `GET /servers` / `GET /services` — requires a `read` (or `read_write`)
  API key. Name/risk/active for every server, or a summary of every
  service across every server -- both paged (`?limit=&offset=`, default
  page size 50, max 500), since either can run into the thousands.
- `GET /servers/{name}` / `GET /services/{id}` — requires a `read` (or
  `read_write`) API key. Everything known about one server (matched
  case-insensitively, including its services) or one service (by numeric
  id, with its server expanded to `{id, name}`); 404 if nothing matches.
- Everything else requires an API key (see below) sent as an `X-API-Key`
  header.

See [API.md](API.md) for the full request/response contract.

## API keys

Two access levels, both stored (hashed, never in plaintext) in the
`api_keys` table in Neon rather than an env var:

- `read` — can call read-only endpoints.
- `read_write` — can call read-only *and* read-write endpoints.

A key is a high-entropy random token, so only its SHA-256 hash is ever
stored (see `api/apilib/auth.py`) — there's no way to recover a lost key,
only revoke it and mint a new one.

## How it's structured

The whole server is one FastAPI app (`api/app.py`), deployed as a single
Vercel serverless function. Vercel's Python framework detection builds the
project as one consolidated function regardless of how many `api/*.py`
files exist, so every route lives in that one module rather than being
split across files (see the sibling `x402-analysis-gui` project for a
longer account of that gotcha).

Shared code (`apilib/`) lives *inside* `api/`, not at the project root, and
is deliberately not named `lib`: the repo's root `.gitignore` has a bare
`lib/` rule (standard Python-venv boilerplate, matching a directory of that
name at any depth), so a package called `lib/` anywhere in this repo never
actually gets committed — it works fine locally, then breaks in production
with `ModuleNotFoundError: No module named 'lib'`, because the module was
silently never pushed. Keeping shared code under `api/` (rather than the
project root) is also a good idea on its own merits, since Vercel's Python
build only reliably bundles files under the entrypoint's own directory
tree — but the name change is what actually avoids the failure.

`api/assets/` (favicon, source icon) lives under `api/` for the same
reason as `apilib/` above -- so it's guaranteed to be part of what Vercel
actually deploys.

```
x402-analysis-api/
  api/
    app.py               every route: /, /status, /favicon.ico
    assets/
      icon.png              source icon
      favicon.ico            generated from icon.png -- served at /favicon.ico
      favicon-16.png          \_ generated alongside favicon.ico, not
      favicon-32.png          /  currently referenced by any route
    apilib/
      db.py                Neon access: the api_keys table + the connection
                           check GET /status uses
      auth.py               API-key hashing + the require_read_access /
                            require_read_write_access route dependencies
      app_setup.py           wires request logging onto the app
  db/
    schema.sql             every table definition (for a brand-new database)
    migrations.sql          idempotent ALTER statements that patch an
                           already-created database up to schema.sql's
                           current shape -- both are read only by
                           scripts/init_db.py, never the deployed function
  scripts/
    preflight.py              stdlib-only shared checks (env vars, Neon
                              connectivity) used by every script below to
                              report everything missing/broken up front, in
                              one message, before doing any real work
    db_setup.py              shared: applies schema.sql then migrations.sql
                             (used by both scripts below, so neither can run
                             against a database that's a migration behind)
    init_db.py                creates/updates the tables in your Neon database
    create_api_key.py          mint/list/revoke API keys
    load_facilitators.py        loads scripts/data/x402Fac.json into the
                                facilitator/uris/addresses/linkaddresses tables
    check_supported.py           calls GET {api_url}/supported for every
                                 facilitator with an api URL on file, and
                                 reports any that didn't return valid JSON
    cdp_client.py                 Coinbase CDP Platform API client, used by
                                  check_supported.py -- vendored from the
                                  sibling cli-tool project's
                                  x402tool/cdp_client.py
    cdp_auth.py                    CDP bearer JWT signing, used by
                                   cdp_client.py -- vendored from
                                   x402tool/cdp_auth.py
    download_db.py                 downloads the entire Neon database into
                                   a local Postgres mirror (localdb/,
                                   gitignored) -- used by update.py, or run
                                   directly to refresh a stale local copy
    x402scan_scraper.py             scrapes x402scan.com's Facilitators
                                    and Servers pages, used by update.py --
                                    vendored from x402tool/x402scan_scraper.py
    service_classifier.py            keyword-based service categoriser,
                                     used by update.py -- vendored from
                                     cli-tool's service_classifier.py
    update.py                      refreshes facilitator/server/service
                                   data from x402scan.com and Coinbase's
                                   CDP Bazaar, and re-checks which
                                   facilitators/services are live, writing
                                   any changes to both localdb/ and Neon
    data/
      x402Fac.json            facilitator data to load (name, API/doc/
                              x402scan URLs, on-chain addresses) -- see
                              cli-tool/tempdata/combine_facilitators.py for
                              how this gets built
  vercel.json
  requirements.txt
  .env.example
  API.md
```

## x402 ecosystem data

Beyond `api_keys`, `db/schema.sql` also defines the actual x402 ecosystem
data model: `uris` (a URL plus what's known about it -- IP, geolocation,
TLS certificate subject), `facilitator`, `server`, `service`, `clients`,
`addresses`, and `linkaddresses` (a polymorphic link between one address
and one facilitator/server/client/funder/associate -- see the comments in
`db/schema.sql` for the full field-by-field rationale).

Right now the only loader is `scripts/load_facilitators.py`, which reads
`scripts/data/x402Fac.json` and populates `facilitator`, `uris`,
`addresses`, and `linkaddresses`. It brings the database's schema up to
date itself before touching any data (same as `scripts/init_db.py` -- see
`scripts/db_setup.py`), so there's no separate "remember to migrate first"
step:

```bash
python scripts/load_facilitators.py
```

For every URL it encounters (a facilitator's API, doc, and x402scan.com
page), it also passively fingerprints the host -- IP address, IP
geolocation, and TLS certificate subject name -- reusing the sibling
`cli-tool` project's `x402tool.net_analysis` module (DNS resolution, a TLS
handshake, and a batched `ip-api.com` lookup), so `cli-tool/` needs to be
checked out next to this project (as it is in this monorepo) for the
import to resolve. This is a local-only maintenance script, never part of
the deployed Vercel function.

The loader is idempotent: re-running it (e.g. after refreshing
`x402Fac.json`, or just to re-fingerprint URLs) upserts by each table's
natural key (`uris.url`, `facilitator.name`, `addresses.address`,
`linkaddresses(owner, ref, address)`) rather than duplicating rows, and
deliberately leaves every `risk`/`active`/`notes` field alone on a
re-import -- those are for manual curation (e.g. a future admin UI), not
this script's concern; only the columns it actually owns (URLs, IP/geo/TLS
fingerprints, address chains/source) get refreshed.

`scripts/check_supported.py` spot-checks that every facilitator's API is
actually alive and speaking x402: for each facilitator with an `api` URL on
file, it calls `GET {api_url}/supported` (the same endpoint the sibling
`cli-tool` project's `GenericFacilitatorClient.supported()` hits) and
reports any facilitator whose response wasn't valid JSON -- printing the
facilitator's name, the URL called, and the raw text that came back (or a
description of the failure, e.g. a timeout or connection error, if no
response was received at all).

```bash
python scripts/check_supported.py
```

Coinbase's CDP Platform API is a special case, since it's authenticated and
versioned (`/v2/x402/supported`, not a bare `/supported`) rather than the
plain contract every other facilitator uses -- the script detects its host
and calls it via `CdpClient` (`scripts/cdp_client.py`) instead, which signs
a short-lived bearer JWT per request (`scripts/cdp_auth.py`). Both files are
vendored, byte-for-byte, from the sibling `cli-tool` project's
`x402tool/cdp_client.py` and `x402tool/cdp_auth.py`, so this script doesn't
need `cli-tool` checked out next to this project just to authenticate to
Coinbase -- their only extra dependencies, `pyjwt` and `cryptography`, are
in this project's own `requirements.txt`.

That needs a CDP API key pair (`CDP_API_KEY_ID` / `CDP_API_KEY_SECRET`, free
from [portal.cdp.coinbase.com](https://portal.cdp.coinbase.com/) -- read
from a `.env` in either this project's root or `cli-tool`'s, so an existing
`cli-tool/.env`, if you already use the CLI against Coinbase, doesn't need
to be duplicated here). Both are **required** to run the script at all:
checked once at startup, before anything else -- if either is missing, the
script prints setup instructions and exits immediately, rather than only
surfacing Coinbase as one of possibly several failures partway through the
run.

`scripts/download_db.py` downloads the *entire* Neon database into a local
PostgreSQL server, so `update.py` (below) can read/write against a local
mirror rather than hitting Neon on every query:

```bash
python scripts/download_db.py
```

The local server is a separate PostgreSQL install/data directory (`initdb`
is run the first time this is needed), kept on its own port (5433 by
default -- `LOCAL_DB_PORT` to override) so it doesn't collide with a system
Postgres on 5432, with everything it creates under `localdb/` --
gitignored, not something to commit. It looks for `psql`/`pg_dump`/
`initdb`/`pg_ctl`/`pg_isready` on `PATH` or a few common Homebrew install
locations; it doesn't install PostgreSQL itself (`brew install
postgresql@16` if you don't have it). Every run is a full, clean mirror --
the local database is dropped and recreated first, so re-running this to
refresh a stale local copy never leaves stray objects behind.

`scripts/update.py` refreshes facilitator data from x402scan.com and
re-checks which facilitators are actually live, writing any changes to
*both* the local mirror and Neon:

```bash
python scripts/update.py
```

1. Downloads the local database first if it hasn't been already (or if the
   local mirror was downloaded from a *different* `DATABASE_URL` than the
   one currently configured -- see below for why that specific check
   exists).
2. Scrapes x402scan.com's Facilitators page (`scripts/x402scan_scraper.py`,
   vendored byte-for-byte from the sibling `cli-tool` project's
   `x402tool/x402scan_scraper.py`, so this doesn't need `cli-tool` checked
   out next to this project either).
3. Compares the scrape against the local database: a facilitator x402scan
   knows about that the database doesn't becomes a new row (its `api` URL
   is left unset -- x402scan's own data doesn't include one, only a doc URL
   and its x402scan.com page URL); a doc/x402scan URL that's changed is
   updated; an address x402scan lists that isn't already linked to that
   facilitator is added (never removed, on the same "additive only"
   principle `load_facilitators.py` follows).
4. Re-checks every facilitator's liveness via `check_supported.py`'s own
   `check_supported()` function (imported directly, not shelled out to): no
   `api` URL, or an `api` URL whose `/supported` isn't valid JSON, means
   not active; anything else does. Wherever that disagrees with what's
   stored, `active` is updated.
5. Scans for servers and the services (resources) they offer, from two
   sources merged by hostname (the same real server can turn up in both,
   but only x402scan gives it a proper name): x402scan.com's homepage
   listing (`x402scan_scraper.scrape_all()` -- vendored already, just not
   previously used for anything but facilitators) and Coinbase's CDP Bazaar
   (`GET /v2/x402/discovery/resources`, unauthenticated -- no CDP
   credentials needed for this part). A server neither database knows about
   becomes a new row; a service (path) not already recorded under its
   server is added, categorised with `scripts/service_classifier.py`'s
   keyword rules (vendored from `cli-tool`, same treatment as
   `cdp_client.py`/`x402scan_scraper.py`), with an `active` flag based on
   whether a plain, unpaid request to it got back HTTP 402 (the correct
   response from a live x402-gated endpoint -- confirming one actually
   works would require a real payment). Existing services aren't touched;
   an existing server is only ever upgraded to active by a newly found live
   service, never downgraded.
6. Every change from steps 3-5 is applied to both databases -- computed
   once against the local mirror, then replayed identically against each,
   since each resolves its own foreign keys by natural key (url/address/
   name) rather than a shared numeric id.

**This local/Neon pairing is the one thing to be careful with.** The diff
in step 3 is only safe to apply to Neon if the local mirror it was computed
against actually reflects that Neon database -- if `DATABASE_URL` ever
points somewhere else than whatever the local mirror was last downloaded
from (a different Neon project/branch, or a stale mirror left over from
testing against a throwaway database), every facilitator Neon already has
but the wrong local mirror doesn't looks "new," and applying that diff
overwrites those facilitators' real `api`/`doc`/`x402scan` with whatever
the scrape/local-mirror side had (nothing, for `api`). That's not
hypothetical -- it's exactly what happened once during this script's own
development, wiping the `api` URL (and flipping `active` to false) for nine
real facilitators in production before it was caught and fixed by hand.
`download_db.is_downloaded(neon_url)` now guards against exactly this: it
checks the local mirror was downloaded from this *exact* `DATABASE_URL`
(comparing a hash recorded at download time, not just "some download
happened at some point") and forces a fresh download whenever that doesn't
match, rather than silently diffing against whatever's already on disk.

### Preflight checks

`update.py` and the scripts it calls check everything they need up front --
env vars, libraries, external tools, Neon connectivity -- and report every
problem found together in one message, rather than failing with a
traceback (or worse, a partially-applied change) partway through a run:

- **Required packages** (`psycopg2`, `requests`, `python-dotenv`, `pyjwt`,
  `cryptography`): each script guards its own imports of these in a
  `try`/`except ImportError`, printing which package is missing and
  `pip install -r requirements.txt` as the fix, then exiting immediately --
  Python can't partially import a module anyway, so there's nothing to
  usefully aggregate here.
- **The sibling `cli-tool` project** checked out next to this one (needed
  by `load_facilitators.fingerprint_urls`, and so transitively by
  `update.py`): same treatment, checked at the point that import happens.
- **`DATABASE_URL`** (Neon): checked for both presence and actual
  connectivity (`preflight.require_database_url`).
- **`CDP_API_KEY_ID`/`CDP_API_KEY_SECRET`**: `check_supported.py`'s
  `preflight_checks()`.
- **PostgreSQL binaries and local port availability**: `download_db.py`'s
  `preflight_checks()`.

`update.py`'s `main()` runs all of these together via `preflight.py` (the
one piece that's genuinely worth aggregating, since these problems are
independent of each other) before doing anything else; `download_db.py`
and `check_supported.py` each run their own subset the same way when run
standalone.

## Neon database setup

1. Create a project at [console.neon.tech](https://console.neon.tech/) (the
   free tier is plenty for this).
2. From the project's **Connection Details**, copy the **pooled** connection
   string (hostname contains `-pooler`) into `DATABASE_URL`. This app opens
   a fresh connection per request — appropriate for Vercel's serverless
   functions — so the pooled string lets Neon's own PgBouncer absorb that
   connect/disconnect churn.
3. Create (or update) the tables:
   ```bash
   python scripts/init_db.py
   ```
   Runs `db/schema.sql` (`CREATE TABLE IF NOT EXISTS`, for a brand-new
   database) then `db/migrations.sql` (idempotent `ALTER TABLE` statements
   that patch an already-created database up to schema.sql's current
   shape -- `CREATE TABLE IF NOT EXISTS` alone can't add a column or
   constraint to a table that already exists). Safe to re-run any time,
   against a database in any of those states.
4. Mint your first API key(s):
   ```bash
   python scripts/create_api_key.py --access read --label "example read-only consumer"
   python scripts/create_api_key.py --access read_write --label "example read-write consumer"
   ```
   Each prints the raw key exactly once — store it now, since only its
   hash is kept. Run `python scripts/create_api_key.py --help` for the
   full set of options (`--list`, `--revoke <id>`).

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in .env: DATABASE_URL

python scripts/init_db.py
python scripts/create_api_key.py --access read_write --label "local dev"

uvicorn api.app:app --reload --port 8000
```

Then:
```bash
curl http://localhost:8000/
curl http://localhost:8000/status
```

## Deploying to Vercel

1. Push this directory to a GitHub repo (or connect the monorepo and set
   this directory as the project's **Root Directory** in Vercel's project
   settings).
2. In the Vercel project's **Settings → Environment Variables**, add
   `DATABASE_URL`.
3. Deploy. Vercel auto-detects the Python/FastAPI app from
   `requirements.txt` + `api/app.py` — no extra build config needed beyond
   `vercel.json`.
4. Run `scripts/init_db.py` and `scripts/create_api_key.py` locally
   (pointed at the same `DATABASE_URL` you set on Vercel) — there's no
   in-app UI for either.
