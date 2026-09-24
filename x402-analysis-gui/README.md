# x402-analysis-gui

A minimal Vercel website with Auth0-gated access, adapted from the same
pattern used by the sibling `worcadian-agent` project. Who's allowed to log
in, their role, and their theme preference live in a Neon Postgres database
rather than an env var.

- `/` — public landing page, no login needed.
- `/dashboard` — Auth0-gated. A header bar (logo, site name, your email, your
  user type, a hamburger menu) plus tabs (Servers/Services/Facilitators/
  Clients/Funders), each currently showing placeholder content.
- `/settings` — Auth0-gated. Currently just one control: your light/dark/
  auto theme preference, stored in the database and applied on every page
  load from then on.

## How it's structured

The whole site is one FastAPI app (`api/app.py`), deployed as a single
Vercel serverless function. Vercel's Python framework detection builds the
project as one consolidated function regardless of how many `api/*.py`
files exist, so every route lives in that one module rather than being
split across files.

```
x402-analysis-gui/
  index.html          public landing page
  dashboard.html      the tabbed, Auth0-gated home page
  settings.html        the Auth0-gated light-mode settings page
  api/
    app.py             every route: /, /auth/login, /auth/callback,
                       /auth/logout, /dashboard, /settings,
                       /settings/light-mode
  gui/
    oauth.py            Auth0 login/callback/logout
    db.py               Neon access: find/add/update/remove/list users
    crypto.py            AES-256-GCM encryption + lookup-hash for the
                         email column
    session.py           signed-cookie session middleware config
    app_setup.py          wires session middleware + request logging onto the app
  db/
    schema.sql          the user_settings table definition
  scripts/
    init_db.py           creates the table in your Neon database
    add_user.py           add/update/remove/list authorised users
  vercel.json
  requirements.txt
  .env.example
```

## Auth0 setup

1. At [manage.auth0.com](https://manage.auth0.com/), create a **Regular Web
   Application**.
2. In its Settings tab, set:
   - **Allowed Callback URLs**: `<PUBLIC_BASE_URL>/auth/callback`
   - **Allowed Logout URLs**: `<PUBLIC_BASE_URL>`

   where `PUBLIC_BASE_URL` is your deployed site's base URL (e.g.
   `https://x402-analysis-gui.vercel.app`), no trailing slash — must match
   the `PUBLIC_BASE_URL` env var exactly.
3. Copy the application's **Domain**, **Client ID**, and **Client Secret**
   into your env vars (see `.env.example`).
4. Generate a `SESSION_SECRET_KEY`:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

A successful Auth0 login only grants access if that email is already in the
`user_settings` table (see below) — Auth0 authenticates *who you are*,
Neon decides *whether you're let in*.

## Neon database setup

1. Create a project at [console.neon.tech](https://console.neon.tech/) (the
   free tier is plenty for this).
2. From the project's **Connection Details**, copy the **pooled** connection
   string (hostname contains `-pooler`) into `DATABASE_URL`. This app opens
   a fresh connection per request — appropriate for Vercel's serverless
   functions — so the pooled string lets Neon's own PgBouncer absorb that
   connect/disconnect churn.
3. Generate the encryption key (see below), then create the table:
   ```bash
   python scripts/init_db.py
   ```
   This runs `db/schema.sql` (`CREATE TABLE IF NOT EXISTS`), so it's safe
   to re-run.
4. Add yourself as the first Admin user:
   ```bash
   python scripts/add_user.py you@example.com --user-type admin
   ```
   Run `python scripts/add_user.py --help` for the full set of options
   (`--user-type {admin,advanced,standard}`, `--light-mode {auto,light,dark}`,
   `--remove`, `--list`).

### Encryption

The `email` and `user_type` columns in `user_settings` are encrypted at rest
with AES-256-GCM before being written to Neon (see `gui/crypto.py`) — Neon's
own infrastructure-level encryption protects the whole database at rest, but
this adds an application-level layer on the columns that are personally
identifying or access-control-sensitive. Lookups (`WHERE email = ...` at
login) go via a separate deterministic HMAC-SHA256 hash of the normalized
email instead of the (randomized, non-searchable) ciphertext; `user_type`
needs no such lookup hash since nothing ever queries by its value — it's
decrypted for display whenever a user's row is read. Both the AES-GCM key
and the HMAC key are derived from a single 256-bit master key via
HKDF-SHA256, so the same secret is never reused across two different
cryptographic purposes.

Generate the master key:
```bash
python -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```
Set it as `DB_ENCRYPTION_KEY`. **Losing this key makes every stored email and
user type unrecoverable** — there's no way to look up or display who a row
belongs to, or what role they have, without it. Back it up somewhere safe
outside of Vercel/Neon themselves.

### Schema

```sql
CREATE TABLE user_settings (
    id                   SERIAL PRIMARY KEY,
    email_hash           TEXT NOT NULL UNIQUE,   -- HMAC-SHA256(email), for lookups
    email_encrypted      TEXT NOT NULL,          -- AES-256-GCM ciphertext
    user_type_encrypted  TEXT NOT NULL,          -- AES-256-GCM ciphertext of "0"/"1"/"2" (0=Admin, 1=Advanced, 2=Standard)
    light_mode           SMALLINT NOT NULL DEFAULT 0,  -- 0=auto, 1=light, 2=dark
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in .env: PUBLIC_BASE_URL=http://localhost:8000, DATABASE_URL,
# DB_ENCRYPTION_KEY, etc.

python scripts/init_db.py
python scripts/add_user.py you@example.com --user-type admin

uvicorn api.app:app --reload --port 8000
```

Then open `http://localhost:8000`.

## Deploying to Vercel

1. Push this directory to a GitHub repo (or connect the monorepo and set
   this directory as the project's **Root Directory** in Vercel's project
   settings).
2. In the Vercel project's **Settings → Environment Variables**, add
   `AUTH0_DOMAIN`, `AUTH0_CLIENT_ID`, `AUTH0_CLIENT_SECRET`,
   `PUBLIC_BASE_URL` (your Vercel deployment URL), `SESSION_SECRET_KEY`,
   `DATABASE_URL`, and `DB_ENCRYPTION_KEY`.
3. Deploy. Vercel auto-detects the Python/FastAPI app from
   `requirements.txt` + `api/app.py` — no extra build config needed beyond
   `vercel.json`.
4. Update the Auth0 application's Allowed Callback/Logout URLs to match the
   real deployed `PUBLIC_BASE_URL` (do this before or right after your first
   deploy — login won't work until they match exactly).
5. Run `scripts/init_db.py` and `scripts/add_user.py` locally (pointed at
   the same `DATABASE_URL`/`DB_ENCRYPTION_KEY` you set on Vercel) — there's
   no in-app UI for either yet.
