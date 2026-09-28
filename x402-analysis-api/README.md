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
    db_setup.py              shared: applies schema.sql then migrations.sql
                             (used by both scripts below, so neither can run
                             against a database that's a migration behind)
    init_db.py                creates/updates the tables in your Neon database
    create_api_key.py          mint/list/revoke API keys
    load_facilitators.py        loads scripts/data/x402Fac.json into the
                                facilitator/uris/addresses/linkaddresses tables
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
