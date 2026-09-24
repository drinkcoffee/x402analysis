"""The entire FastAPI app, deployed as ONE Vercel serverless function.

Vercel's "FastAPI" framework detection builds the whole project as a single
consolidated serverless function, not one function per api/*.py file -- so
every route for the whole site lives in this one module, using plain,
standard FastAPI paths. See the sibling worcadian-agent project (which this
one's structure is adapted from) for a longer account of that gotcha.

Routes:
    GET  /                    public landing page (index.html), no login needed
    GET  /auth/login           starts the Auth0 login flow
    GET  /auth/callback        Auth0 redirects back here with the auth code
    GET  /auth/logout          clears the session + Auth0 logout
    GET  /dashboard            the OAuth-gated page (dashboard.html) -- header
                               bar (logo, site name, user type, the logged-in
                               user's email, a hamburger menu with Settings +
                               Log out) plus tabs
                               (Servers/Services/Facilitators/Clients/Funders),
                               each currently showing placeholder content
    GET  /settings             the OAuth-gated settings page -- currently just
                               the light/dark/auto theme preference
    POST /settings/light-mode  updates the current user's light_mode
    GET  /assets/*             static files (favicons, the site logo) from
                               assets/, mounted via StaticFiles

"Who's allowed to log in" and each user's role/theme preference live in the
`user_settings` table in Neon (gui/db.py) -- not an env var allowlist. A
successful Auth0 login only grants access if that email is already in the
table; see scripts/add_user.py for how to add one.

Logging: configured (via gui.app_setup.configure_app) so every logger.info()/
logger.exception() call in this module reaches stderr, which Vercel captures
as Function Logs.
"""

from __future__ import annotations

import logging
import sys
from html import escape
from pathlib import Path
from typing import Optional

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

import gui.db as db  # noqa: E402
from gui.app_setup import configure_app  # noqa: E402
from gui.oauth import (  # noqa: E402
    build_authorize_url,
    build_logout_url,
    exchange_code_for_email,
    new_state,
)

app = FastAPI()
configure_app(app, logger)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML_PATH = PROJECT_ROOT / "index.html"
DASHBOARD_HTML_PATH = PROJECT_ROOT / "dashboard.html"
SETTINGS_HTML_PATH = PROJECT_ROOT / "settings.html"

# Site icon/logo (favicon variants + the logo shown on the landing page).
app.mount("/assets", StaticFiles(directory=str(PROJECT_ROOT / "assets")), name="assets")


def _theme_attr(light_mode: int) -> str:
    """The `<html ...>` attribute that forces light/dark, or "" for auto
    (which just leaves the page's `@media (prefers-color-scheme)` rule to
    follow the OS/browser setting, undisturbed)."""
    if light_mode == db.LIGHT_MODE_LIGHT:
        return ' data-theme="light"'
    if light_mode == db.LIGHT_MODE_DARK:
        return ' data-theme="dark"'
    return ""


def _require_user(request: Request) -> tuple[Optional[str], Optional[dict]]:
    """(email, user_settings_row) for the current session, or (email, None)
    if there's no session or that email is no longer an authorised user."""
    email = request.session.get("user_email")
    if not email:
        return None, None
    user = db.find_user_by_email(email)
    return email, user


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

    if not db.find_user_by_email(email):
        logger.warning("oauth callback: email=%s is not in user_settings", email)
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


# --- OAuth-gated pages ---------------------------------------------------------


@app.get("/dashboard")
def dashboard(request: Request):
    email, user = _require_user(request)
    if not user:
        logger.info("dashboard: no valid session (email=%s); redirecting to the public landing page", email)
        return RedirectResponse(url="/")
    page = DASHBOARD_HTML_PATH.read_text()
    page = page.replace("{{THEME_ATTR}}", _theme_attr(user["light_mode"]))
    page = page.replace("{{USER_EMAIL}}", escape(email))
    return HTMLResponse(page)


@app.get("/settings")
def settings_page(request: Request):
    email, user = _require_user(request)
    if not user:
        logger.info("settings: no valid session (email=%s); redirecting to the public landing page", email)
        return RedirectResponse(url="/")
    page = SETTINGS_HTML_PATH.read_text()
    page = page.replace("{{THEME_ATTR}}", _theme_attr(user["light_mode"]))
    page = page.replace("{{USER_EMAIL}}", escape(email))
    for mode, placeholder in (
        (db.LIGHT_MODE_AUTO, "AUTO_SELECTED"),
        (db.LIGHT_MODE_LIGHT, "LIGHT_SELECTED"),
        (db.LIGHT_MODE_DARK, "DARK_SELECTED"),
    ):
        page = page.replace("{{" + placeholder + "}}", "selected" if user["light_mode"] == mode else "")
    return HTMLResponse(page)


@app.post("/settings/light-mode")
async def update_light_mode(request: Request):
    email, user = _require_user(request)
    if not user:
        logger.info("settings/light-mode: no valid session (email=%s)", email)
        return RedirectResponse(url="/", status_code=303)

    form = await request.form()
    try:
        light_mode = int(form.get("light_mode", ""))
    except (TypeError, ValueError):
        light_mode = db.LIGHT_MODE_AUTO
    if light_mode not in db.VALID_LIGHT_MODES:
        light_mode = db.LIGHT_MODE_AUTO

    db.update_light_mode(email, light_mode)
    logger.info("settings/light-mode: email=%s set light_mode=%s", email, light_mode)
    return RedirectResponse(url="/settings", status_code=303)
