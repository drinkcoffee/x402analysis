# x402-analysis-api

A minimal Vercel API server (Python/FastAPI) backed by a Neon Postgres
database, providing programmatic access to x402 ecosystem analysis data.

This file covers running and deploying the server. For the
request/response contract each endpoint exposes, see [API.md](API.md).

## Routes

- `GET /` — unauthenticated. A one-line pointer to `API.md`.
- `GET /status` — unauthenticated. Checks the Neon connection and reports
  whether the server and database are online.
- Everything else requires an API key (see below) sent as an `X-API-Key`
  header. There are no other endpoints yet.

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

```
x402-analysis-api/
  api/
    app.py               every route: /, /status
    apilib/
      db.py                Neon access: the api_keys table + the connection
                           check GET /status uses
      auth.py               API-key hashing + the require_read_access /
                            require_read_write_access route dependencies
      app_setup.py           wires request logging onto the app
  db/
    schema.sql             the api_keys table definition (read only by
                           scripts/init_db.py, not the deployed function)
  scripts/
    init_db.py              creates the table in your Neon database
    create_api_key.py        mint/list/revoke API keys
  vercel.json
  requirements.txt
  .env.example
  API.md
```

## Neon database setup

1. Create a project at [console.neon.tech](https://console.neon.tech/) (the
   free tier is plenty for this).
2. From the project's **Connection Details**, copy the **pooled** connection
   string (hostname contains `-pooler`) into `DATABASE_URL`. This app opens
   a fresh connection per request — appropriate for Vercel's serverless
   functions — so the pooled string lets Neon's own PgBouncer absorb that
   connect/disconnect churn.
3. Create the table:
   ```bash
   python scripts/init_db.py
   ```
   This runs `db/schema.sql` (`CREATE TABLE IF NOT EXISTS`), so it's safe
   to re-run.
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
