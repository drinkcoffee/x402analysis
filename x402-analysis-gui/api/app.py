"""The entire FastAPI app, deployed as ONE Vercel serverless function.

Vercel's "FastAPI" framework detection builds the whole project as a single
consolidated serverless function, not one function per api/*.py file -- so
every route for the whole site lives in this one module, using plain,
standard FastAPI paths. See the sibling worcadian-agent project (which this
one's structure is adapted from) for a longer account of that gotcha.

Routes:
    GET  /                    public landing page (index.html), no login needed
    GET  /auth/login           starts the Auth0 login flow
    GET  /auth/signup          starts the Auth0 signup flow (screen_hint=signup).
                               If the signed-up email isn't already in
                               user_settings, emails ADMIN_NOTIFY_EMAIL via
                               Resend (gui/email_notify.py) requesting approval,
                               instead of the plain "access denied" a /auth/login
                               attempt with an unapproved email gets.
    GET  /auth/callback        Auth0 redirects back here with the auth code.
                               A successful /auth/login for an unapproved
                               email lands on /request-access instead of a
                               flat "access denied" message.
    GET  /auth/logout          clears the session + Auth0 logout
    GET  /request-access       shown after a successful Auth0 login whose
                               email isn't in user_settings yet -- a "Request
                               Access" button for that email (kept in the
                               session as pending_access_email, not a URL
                               param, so it can't be edited client-side).
    POST /auth/request-access  sends the same Resend approval-request email
                               /auth/signup sends automatically, but only
                               when the user explicitly clicks that button
    GET  /dashboard            the OAuth-gated page (dashboard.html) -- header
                               bar (logo, site name, the logged-in user's
                               email, their user type, a hamburger menu with
                               Settings + Log out) plus tabs
                               (Servers/Services/Facilitators/Clients/Funders),
                               each currently showing placeholder content
    GET  /settings             the OAuth-gated settings page -- currently just
                               the light/dark/auto theme preference
    POST /settings/light-mode  updates the current user's light_mode
    GET  /admin/users          Admin-only: add/list authorised users
                               (user_admin.html). Non-admins are redirected
                               to /dashboard; the "User Administration" menu
                               item itself is only shown to Admins.
    POST /admin/users          Admin-only: adds (or updates the user_type of)
                               an authorised user, with light_mode defaulting
                               to auto
    POST /admin/users/update   Admin-only: changes an existing user's
                               user_type (the pencil-button edit modal on
                               /admin/users). An admin can't target their
                               own account this way -- there's no edit
                               button next to their own row, and the route
                               itself ignores such a request too.
    POST /admin/users/delete   Admin-only: removes an authorised user (same
                               edit modal, same can't-target-yourself rule).
    GET  /risk-score-factors   Admin/Advanced only: a placeholder page
                               (risk_score_factors.html). Standard users
                               don't see the "Risk Score Factors" menu item
                               and are redirected to /dashboard if they hit
                               the URL directly.
    GET  /facilitator          OAuth-gated (any authenticated user):
                               facilitator.html, a detail page for one row
                               of the dashboard's Facilitators tab, reached
                               by clicking that row. Which facilitator to
                               show is passed as ?name=... and fetched
                               client-side from GET /api/facilitators/{name}.
    GET  /server                OAuth-gated: server.html, a detail page for
                                one row of the dashboard's Servers tab,
                                reached by clicking that row (?name=...,
                                fetched from GET /api/servers/{name}) --
                                including the services that server offers.
    GET  /service                OAuth-gated: service.html, a detail page
                                 for one row of the dashboard's Services tab
                                 or of a server's own services table
                                 (?id=..., fetched from GET /api/services/{id}).
    GET  /api/facilitators          OAuth-gated JSON proxy to the sibling
                                    x402-analysis-api server's own
                                    GET /facilitators (see gui/facilitators_client.py)
                                    -- name/risk/active for every facilitator.
                                    Proxying server-side, rather than having
                                    the browser call x402-analysis-api
                                    directly, is what keeps X402_API_KEY out
                                    of client-side JavaScript.
    GET  /api/facilitators/{name}   OAuth-gated JSON proxy to the sibling
                                    server's GET /facilitators/{name} --
                                    everything known about one facilitator;
                                    404 if none match.
    GET  /api/servers               OAuth-gated JSON proxy to the sibling
                                    server's GET /servers (see
                                    gui/servers_client.py) -- name/risk/
                                    active for a page of servers (?limit=&
                                    offset=; there can be thousands).
    GET  /api/servers/{name}        OAuth-gated JSON proxy to GET
                                    /servers/{name} -- everything known
                                    about one server, including its
                                    services; 404 if none match.
    GET  /api/services               OAuth-gated JSON proxy to the sibling
                                     server's GET /services (see
                                     gui/services_client.py) -- a summary of
                                     a page of services across every server
                                     (?limit=&offset=; there can be
                                     thousands), optionally filtered to one
                                     or more categories (repeat
                                     ?category=...).
    GET  /api/services/categories     OAuth-gated JSON proxy to GET
                                      /services/categories -- every
                                      distinct category currently assigned
                                      to at least one service; the option
                                      list for the filter above.
    GET  /api/services/{id}          OAuth-gated JSON proxy to GET
                                     /services/{id} -- everything known
                                     about one service; 404 if none match.
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

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

import gui.db as db  # noqa: E402
import gui.facilitators_client as facilitators_client  # noqa: E402
import gui.servers_client as servers_client  # noqa: E402
import gui.services_client as services_client  # noqa: E402
from gui.app_setup import configure_app  # noqa: E402
from gui.email_notify import send_access_request  # noqa: E402
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
USER_ADMIN_HTML_PATH = PROJECT_ROOT / "user_admin.html"
RISK_SCORE_HTML_PATH = PROJECT_ROOT / "risk_score_factors.html"
FACILITATOR_HTML_PATH = PROJECT_ROOT / "facilitator.html"
SERVER_HTML_PATH = PROJECT_ROOT / "server.html"
SERVICE_HTML_PATH = PROJECT_ROOT / "service.html"
REQUEST_ACCESS_HTML_PATH = PROJECT_ROOT / "request_access.html"

# GET /api/servers and GET /api/services can each have thousands of rows,
# so both are paged (?limit=&offset=) rather than returned whole the way
# GET /api/facilitators is -- there are only ever a few dozen facilitators.
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 500

_ACCESS_REQUEST_SENT_HTML = (
    "<p>Thanks! Your request for access has been sent to the site "
    "administrator. You'll be able to log in once it's approved.</p>"
)

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


def _require_admin(request: Request) -> tuple[Optional[str], Optional[dict]]:
    """Like _require_user, but also returns (email, None) if the signed-in
    user isn't an Admin."""
    email, user = _require_user(request)
    if user and user["user_type"] != db.USER_TYPE_ADMIN:
        return email, None
    return email, user


def _require_admin_or_advanced(request: Request) -> tuple[Optional[str], Optional[dict]]:
    """Like _require_user, but also returns (email, None) if the signed-in
    user is a Standard user."""
    email, user = _require_user(request)
    if user and user["user_type"] not in (db.USER_TYPE_ADMIN, db.USER_TYPE_ADVANCED):
        return email, None
    return email, user


def _admin_menu_item(user: dict) -> str:
    """The "User Administration" menu link, shown only to Admins."""
    if user["user_type"] != db.USER_TYPE_ADMIN:
        return ""
    return '<a href="/admin/users">User Administration</a>'


def _risk_score_menu_item(user: dict) -> str:
    """The "Risk Score Factors" menu link, shown only to Admins and
    Advanced users."""
    if user["user_type"] not in (db.USER_TYPE_ADMIN, db.USER_TYPE_ADVANCED):
        return ""
    return '<a href="/risk-score-factors">Risk Score Factors</a>'


def _render_user_rows(current_email: str) -> str:
    rows = db.list_users()
    if not rows:
        return '<tr><td colspan="4">(no authorised users yet)</td></tr>'
    row_html = []
    for row in rows:
        is_self = row["email"].strip().lower() == current_email.strip().lower()
        edit_button = (
            ""
            if is_self
            else '<button type="button" class="edit-user-button" data-email="{}" data-user-type="{}" aria-label="Edit {}">&#9998;</button>'.format(
                escape(row["email"], quote=True), row["user_type"], escape(row["email"], quote=True)
            )
        )
        row_html.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                escape(row["email"]),
                escape(db.USER_TYPE_NAMES[row["user_type"]]),
                escape(db.LIGHT_MODE_NAMES[row["light_mode"]]),
                edit_button,
            )
        )
    return "\n".join(row_html)


# --- Public landing page -----------------------------------------------------


@app.get("/")
def landing():
    return HTMLResponse(INDEX_HTML_PATH.read_text())


# --- Auth0 login / callback / logout -----------------------------------------


@app.get("/auth/login")
def login(request: Request):
    state = new_state()
    request.session["oauth_state"] = state
    request.session["oauth_intent"] = "login"
    return RedirectResponse(url=build_authorize_url(state))


@app.get("/auth/signup")
def signup(request: Request):
    state = new_state()
    request.session["oauth_state"] = state
    request.session["oauth_intent"] = "signup"
    return RedirectResponse(url=build_authorize_url(state, screen_hint="signup"))


@app.get("/auth/callback")
def callback(request: Request):
    error = request.query_params.get("error")
    if error:
        logger.warning("oauth callback error=%s", error)
        return HTMLResponse(f"<p>Login failed: {error}</p>", status_code=400)

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    expected_state = request.session.pop("oauth_state", None)
    intent = request.session.pop("oauth_intent", "login")
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
        if intent == "signup":
            send_access_request(email)
            return HTMLResponse(_ACCESS_REQUEST_SENT_HTML)
        request.session["pending_access_email"] = email
        return RedirectResponse(url="/request-access")

    request.session["user_email"] = email
    logger.info("oauth callback: login succeeded email=%s", email)
    return RedirectResponse(url="/dashboard")


@app.get("/auth/logout")
def logout(request: Request):
    email = request.session.get("user_email")
    request.session.clear()
    logger.info("logout: cleared local session for email=%s; redirecting to Auth0 logout", email)
    return RedirectResponse(url=build_logout_url())


@app.get("/request-access")
def request_access_page(request: Request):
    email = request.session.get("pending_access_email")
    if not email:
        return RedirectResponse(url="/")
    page = REQUEST_ACCESS_HTML_PATH.read_text()
    page = page.replace("{{EMAIL}}", escape(email))
    return HTMLResponse(page)


@app.post("/auth/request-access")
def submit_access_request(request: Request):
    email = request.session.pop("pending_access_email", None)
    if not email:
        return RedirectResponse(url="/", status_code=303)
    send_access_request(email)
    logger.info("request-access: sent access-request email for %s", email)
    return HTMLResponse(_ACCESS_REQUEST_SENT_HTML)


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
    page = page.replace("{{USER_TYPE}}", escape(db.USER_TYPE_NAMES[user["user_type"]]))
    page = page.replace("{{RISK_SCORE_MENU_ITEM}}", _risk_score_menu_item(user))
    page = page.replace("{{ADMIN_MENU_ITEM}}", _admin_menu_item(user))
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
    page = page.replace("{{USER_TYPE}}", escape(db.USER_TYPE_NAMES[user["user_type"]]))
    page = page.replace("{{RISK_SCORE_MENU_ITEM}}", _risk_score_menu_item(user))
    page = page.replace("{{ADMIN_MENU_ITEM}}", _admin_menu_item(user))
    for mode, placeholder in (
        (db.LIGHT_MODE_AUTO, "AUTO_SELECTED"),
        (db.LIGHT_MODE_LIGHT, "LIGHT_SELECTED"),
        (db.LIGHT_MODE_DARK, "DARK_SELECTED"),
    ):
        page = page.replace("{{" + placeholder + "}}", "selected" if user["light_mode"] == mode else "")
    return HTMLResponse(page)


@app.get("/admin/users")
def admin_users_page(request: Request):
    email, user = _require_admin(request)
    if not user:
        logger.info("admin/users: no valid session or non-admin (email=%s); redirecting", email)
        return RedirectResponse(url="/dashboard" if email else "/")
    page = USER_ADMIN_HTML_PATH.read_text()
    page = page.replace("{{THEME_ATTR}}", _theme_attr(user["light_mode"]))
    page = page.replace("{{USER_EMAIL}}", escape(email))
    page = page.replace("{{USER_TYPE}}", escape(db.USER_TYPE_NAMES[user["user_type"]]))
    page = page.replace("{{RISK_SCORE_MENU_ITEM}}", _risk_score_menu_item(user))
    page = page.replace("{{ADMIN_MENU_ITEM}}", _admin_menu_item(user))
    page = page.replace("{{USER_ROWS}}", _render_user_rows(email))
    return HTMLResponse(page)


@app.post("/admin/users")
async def admin_add_user(request: Request):
    email, user = _require_admin(request)
    if not user:
        logger.info("admin/users POST: no valid session or non-admin (email=%s)", email)
        return RedirectResponse(url="/dashboard" if email else "/", status_code=303)

    form = await request.form()
    new_email = (form.get("email") or "").strip()
    try:
        new_user_type = int(form.get("user_type", ""))
    except (TypeError, ValueError):
        new_user_type = db.USER_TYPE_STANDARD
    if new_user_type not in db.VALID_USER_TYPES:
        new_user_type = db.USER_TYPE_STANDARD

    if new_email:
        db.add_user(new_email, user_type=new_user_type, light_mode=db.LIGHT_MODE_AUTO)
        logger.info("admin/users POST: admin=%s added/updated user=%s user_type=%s", email, new_email, new_user_type)

    return RedirectResponse(url="/admin/users", status_code=303)


@app.post("/admin/users/update")
async def admin_update_user(request: Request):
    email, user = _require_admin(request)
    if not user:
        logger.info("admin/users/update POST: no valid session or non-admin (email=%s)", email)
        return RedirectResponse(url="/dashboard" if email else "/", status_code=303)

    form = await request.form()
    target_email = (form.get("email") or "").strip()
    try:
        new_user_type = int(form.get("user_type", ""))
    except (TypeError, ValueError):
        new_user_type = None

    if not target_email or target_email.lower() == email.lower():
        logger.warning("admin/users/update POST: admin=%s attempted to edit own account; ignored", email)
    elif new_user_type not in db.VALID_USER_TYPES:
        logger.warning("admin/users/update POST: admin=%s sent invalid user_type for %s; ignored", email, target_email)
    else:
        db.update_user_type(target_email, new_user_type)
        logger.info("admin/users/update POST: admin=%s set user=%s user_type=%s", email, target_email, new_user_type)

    return RedirectResponse(url="/admin/users", status_code=303)


@app.post("/admin/users/delete")
async def admin_delete_user(request: Request):
    email, user = _require_admin(request)
    if not user:
        logger.info("admin/users/delete POST: no valid session or non-admin (email=%s)", email)
        return RedirectResponse(url="/dashboard" if email else "/", status_code=303)

    form = await request.form()
    target_email = (form.get("email") or "").strip()

    if not target_email or target_email.lower() == email.lower():
        logger.warning("admin/users/delete POST: admin=%s attempted to delete own account; ignored", email)
    else:
        db.remove_user(target_email)
        logger.info("admin/users/delete POST: admin=%s deleted user=%s", email, target_email)

    return RedirectResponse(url="/admin/users", status_code=303)


@app.get("/risk-score-factors")
def risk_score_factors_page(request: Request):
    email, user = _require_admin_or_advanced(request)
    if not user:
        logger.info("risk-score-factors: no valid session or standard user (email=%s); redirecting", email)
        return RedirectResponse(url="/dashboard" if email else "/")
    page = RISK_SCORE_HTML_PATH.read_text()
    page = page.replace("{{THEME_ATTR}}", _theme_attr(user["light_mode"]))
    page = page.replace("{{USER_EMAIL}}", escape(email))
    page = page.replace("{{USER_TYPE}}", escape(db.USER_TYPE_NAMES[user["user_type"]]))
    page = page.replace("{{RISK_SCORE_MENU_ITEM}}", _risk_score_menu_item(user))
    page = page.replace("{{ADMIN_MENU_ITEM}}", _admin_menu_item(user))
    return HTMLResponse(page)


@app.get("/facilitator")
def facilitator_page(request: Request):
    email, user = _require_user(request)
    if not user:
        logger.info("facilitator: no valid session (email=%s); redirecting to the public landing page", email)
        return RedirectResponse(url="/")
    page = FACILITATOR_HTML_PATH.read_text()
    page = page.replace("{{THEME_ATTR}}", _theme_attr(user["light_mode"]))
    page = page.replace("{{USER_EMAIL}}", escape(email))
    page = page.replace("{{USER_TYPE}}", escape(db.USER_TYPE_NAMES[user["user_type"]]))
    page = page.replace("{{RISK_SCORE_MENU_ITEM}}", _risk_score_menu_item(user))
    page = page.replace("{{ADMIN_MENU_ITEM}}", _admin_menu_item(user))
    return HTMLResponse(page)


@app.get("/server")
def server_page(request: Request):
    email, user = _require_user(request)
    if not user:
        logger.info("server: no valid session (email=%s); redirecting to the public landing page", email)
        return RedirectResponse(url="/")
    page = SERVER_HTML_PATH.read_text()
    page = page.replace("{{THEME_ATTR}}", _theme_attr(user["light_mode"]))
    page = page.replace("{{USER_EMAIL}}", escape(email))
    page = page.replace("{{USER_TYPE}}", escape(db.USER_TYPE_NAMES[user["user_type"]]))
    page = page.replace("{{RISK_SCORE_MENU_ITEM}}", _risk_score_menu_item(user))
    page = page.replace("{{ADMIN_MENU_ITEM}}", _admin_menu_item(user))
    return HTMLResponse(page)


@app.get("/service")
def service_page(request: Request):
    email, user = _require_user(request)
    if not user:
        logger.info("service: no valid session (email=%s); redirecting to the public landing page", email)
        return RedirectResponse(url="/")
    page = SERVICE_HTML_PATH.read_text()
    page = page.replace("{{THEME_ATTR}}", _theme_attr(user["light_mode"]))
    page = page.replace("{{USER_EMAIL}}", escape(email))
    page = page.replace("{{USER_TYPE}}", escape(db.USER_TYPE_NAMES[user["user_type"]]))
    page = page.replace("{{RISK_SCORE_MENU_ITEM}}", _risk_score_menu_item(user))
    page = page.replace("{{ADMIN_MENU_ITEM}}", _admin_menu_item(user))
    return HTMLResponse(page)


@app.get("/api/facilitators")
def api_list_facilitators(request: Request):
    email, user = _require_user(request)
    if not user:
        return JSONResponse({"detail": "Not authenticated."}, status_code=401)
    try:
        return JSONResponse(facilitators_client.list_facilitators())
    except requests.RequestException:
        logger.exception("api/facilitators: upstream request to x402-analysis-api failed")
        return JSONResponse({"detail": "Facilitators service is unavailable."}, status_code=502)


@app.get("/api/facilitators/{name}")
def api_get_facilitator(name: str, request: Request):
    email, user = _require_user(request)
    if not user:
        return JSONResponse({"detail": "Not authenticated."}, status_code=401)
    try:
        facilitator = facilitators_client.get_facilitator(name)
    except requests.RequestException:
        logger.exception("api/facilitators/%s: upstream request to x402-analysis-api failed", name)
        return JSONResponse({"detail": "Facilitators service is unavailable."}, status_code=502)
    if facilitator is None:
        return JSONResponse({"detail": f"No facilitator named {name!r}."}, status_code=404)
    return JSONResponse(facilitator)


@app.get("/api/servers")
def api_list_servers(
    request: Request,
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
):
    email, user = _require_user(request)
    if not user:
        return JSONResponse({"detail": "Not authenticated."}, status_code=401)
    try:
        return JSONResponse(servers_client.list_servers(limit, offset))
    except requests.RequestException:
        logger.exception("api/servers: upstream request to x402-analysis-api failed")
        return JSONResponse({"detail": "Servers service is unavailable."}, status_code=502)


@app.get("/api/servers/{name}")
def api_get_server(name: str, request: Request):
    email, user = _require_user(request)
    if not user:
        return JSONResponse({"detail": "Not authenticated."}, status_code=401)
    try:
        server = servers_client.get_server(name)
    except requests.RequestException:
        logger.exception("api/servers/%s: upstream request to x402-analysis-api failed", name)
        return JSONResponse({"detail": "Servers service is unavailable."}, status_code=502)
    if server is None:
        return JSONResponse({"detail": f"No server named {name!r}."}, status_code=404)
    return JSONResponse(server)


@app.get("/api/services")
def api_list_services(
    request: Request,
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    category: list[str] = Query([]),
):
    email, user = _require_user(request)
    if not user:
        return JSONResponse({"detail": "Not authenticated."}, status_code=401)
    try:
        return JSONResponse(services_client.list_services(limit, offset, categories=category or None))
    except requests.RequestException:
        logger.exception("api/services: upstream request to x402-analysis-api failed")
        return JSONResponse({"detail": "Services service is unavailable."}, status_code=502)


@app.get("/api/services/categories")
def api_list_service_categories(request: Request):
    email, user = _require_user(request)
    if not user:
        return JSONResponse({"detail": "Not authenticated."}, status_code=401)
    try:
        return JSONResponse(services_client.list_service_categories())
    except requests.RequestException:
        logger.exception("api/services/categories: upstream request to x402-analysis-api failed")
        return JSONResponse({"detail": "Services service is unavailable."}, status_code=502)


@app.get("/api/services/{service_id}")
def api_get_service(service_id: int, request: Request):
    email, user = _require_user(request)
    if not user:
        return JSONResponse({"detail": "Not authenticated."}, status_code=401)
    try:
        service = services_client.get_service(service_id)
    except requests.RequestException:
        logger.exception("api/services/%s: upstream request to x402-analysis-api failed", service_id)
        return JSONResponse({"detail": "Services service is unavailable."}, status_code=502)
    if service is None:
        return JSONResponse({"detail": f"No service with id {service_id}."}, status_code=404)
    return JSONResponse(service)


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
