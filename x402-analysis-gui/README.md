# x402-analysis-gui

A minimal Vercel website with Auth0-gated access, adapted from the same
pattern used by the sibling `worcadian-agent` project.

- `/` — public landing page, no login needed.
- `/dashboard` — Auth0-gated page. Once logged in it just says
  **"You have arrived"**.

## How it's structured

The whole site is one FastAPI app (`api/app.py`), deployed as a single
Vercel serverless function. Vercel's Python framework detection builds the
project as one consolidated function regardless of how many `api/*.py`
files exist, so every route lives in that one module rather than being
split across files.

```
x402-analysis-gui/
  index.html        public landing page
  dashboard.html    "You have arrived" page, served only after login
  api/
    app.py          every route: /, /auth/login, /auth/callback,
                     /auth/logout, /dashboard
  gui/
    oauth.py         Auth0 login/callback/logout + email allowlist
    session.py        signed-cookie session middleware config
    app_setup.py       wires session middleware + request logging onto the app
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
4. Set `ALLOWED_EMAILS` to a comma-separated list of the email addresses
   allowed to log in. Anyone else who successfully authenticates via Auth0
   is still denied access at the callback step.
5. Generate a `SESSION_SECRET_KEY`:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in .env, with PUBLIC_BASE_URL=http://localhost:8000

uvicorn api.app:app --reload --port 8000
```

Then open `http://localhost:8000`.

## Deploying to Vercel

1. Push this directory to a GitHub repo (or connect the monorepo and set
   this directory as the project's **Root Directory** in Vercel's project
   settings).
2. In the Vercel project's **Settings → Environment Variables**, add
   `AUTH0_DOMAIN`, `AUTH0_CLIENT_ID`, `AUTH0_CLIENT_SECRET`,
   `PUBLIC_BASE_URL` (your Vercel deployment URL), `ALLOWED_EMAILS`, and
   `SESSION_SECRET_KEY`.
3. Deploy. Vercel auto-detects the Python/FastAPI app from
   `requirements.txt` + `api/app.py` — no extra build config needed beyond
   `vercel.json`.
4. Update the Auth0 application's Allowed Callback/Logout URLs to match the
   real deployed `PUBLIC_BASE_URL` (do this before or right after your first
   deploy — login won't work until they match exactly).
