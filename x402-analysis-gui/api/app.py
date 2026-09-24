"""The entire FastAPI app, deployed as ONE Vercel serverless function.

Vercel's "FastAPI" framework detection builds the whole project as a single
consolidated serverless function, not one function per api/*.py file -- so
every route for the whole site lives in this one module, using plain,
standard FastAPI paths. See the sibling worcadian-agent project (which this
one's structure is adapted from) for a longer account of that gotcha.

Routes:
    GET  /               public landing page (index.html), no login needed
    GET  /auth/login      starts the Auth0 login flow
    GET  /auth/callback   Auth0 redirects back here with the auth code
    GET  /auth/logout     clears the session + Auth0 logout
    GET  /dashboard       the OAuth-gated page (dashboard.html) -- just says
                          "You have arrived"
    GET  /assets/*        static files (favicons, the site logo) from
                          assets/, mounted via StaticFiles

Logging: configured (via gui.app_setup.configure_app) so every logger.info()/
logger.exception() call in this module reaches stderr, which Vercel captures
as Function Logs.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("x402_gui.api")

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

from gui.app_setup import configure_app  # noqa: E402
from gui.oauth import (  # noqa: E402
    build_authorize_url,
    build_logout_url,
    exchange_code_for_email,
    is_email_allowed,
    new_state,
)

app = FastAPI()
configure_app(app, logger)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML_PATH = PROJECT_ROOT / "index.html"
DASHBOARD_HTML_PATH = PROJECT_ROOT / "dashboard.html"

# Site icon/logo (favicon variants + the logo shown on the landing page).
app.mount("/assets", StaticFiles(directory=str(PROJECT_ROOT / "assets")), name="assets")


# --- Public landing page -----------------------------------------------------


@app.get("/")
def landing():
    return HTMLResponse(INDEX_HTML_PATH.read_text())


# --- Auth0 login / callback / logout -----------------------------------------


@app.get("/auth/login")
def login(request: Request):
    state = new_state()
    request.session["oauth_state"] = state
    return RedirectResponse(url=build_authorize_url(state))


@app.get("/auth/callback")
def callback(request: Request):
    error = request.query_params.get("error")
    if error:
        logger.warning("oauth callback error=%s", error)
        return HTMLResponse(f"<p>Login failed: {error}</p>", status_code=400)

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    expected_state = request.session.pop("oauth_state", None)
    if not code or not state or not expected_state or state != expected_state:
        logger.warning("oauth callback: missing or mismatched state (possible CSRF or expired attempt)")
        return HTMLResponse("<p>Login failed: invalid or expired login attempt. Please try again.</p>", status_code=400)

    try:
        email = exchange_code_for_email(code)
    except Exception:
        logger.exception("oauth callback: token exchange failed")
        return HTMLResponse("<p>Login failed.</p>", status_code=400)

    if not is_email_allowed(email):
        logger.warning("oauth callback: email=%s is not in ALLOWED_EMAILS", email)
        return HTMLResponse("<p>Access denied: this account is not authorized to use this app.</p>", status_code=403)

    request.session["user_email"] = email
    logger.info("oauth callback: login succeeded email=%s", email)
    return RedirectResponse(url="/dashboard")


@app.get("/auth/logout")
def logout(request: Request):
    email = request.session.get("user_email")
    request.session.clear()
    logger.info("logout: cleared local session for email=%s; redirecting to Auth0 logout", email)
    return RedirectResponse(url=build_logout_url())


# --- OAuth-gated page ---------------------------------------------------------


@app.get("/dashboard")
def dashboard(request: Request):
    email = request.session.get("user_email")
    if not email or not is_email_allowed(email):
        logger.info("dashboard: no valid session (email=%s); redirecting to the public landing page", email)
        return RedirectResponse(url="/")
    return HTMLResponse(DASHBOARD_HTML_PATH.read_text())
