"""Auth0 login (OAuth2/OIDC Authorization Code flow) + email allowlist check.

Standard flow against your Auth0 tenant:
    1. build_authorize_url(state) -- send the browser here to log in
    2. Auth0 redirects back to PUBLIC_BASE_URL + "/auth/callback" with
       ?code=...&state=...
    3. exchange_code_for_email(code) -- trades the code for an access token,
       then calls Auth0's /userinfo endpoint to get the verified email
    4. is_email_allowed(email) -- checks it against ALLOWED_EMAILS
    5. build_logout_url() -- also ends the Auth0-side SSO session, not just
       our own cookie (see the /auth/logout route in api/app.py)

Env vars:
    AUTH0_DOMAIN          required, e.g. "your-tenant.us.auth0.com" (no
                          scheme, no trailing slash) -- your Auth0 tenant or
                          custom domain
    AUTH0_CLIENT_ID       required -- from an Auth0 "Regular Web Application"
    AUTH0_CLIENT_SECRET   required
    PUBLIC_BASE_URL       required, e.g. "https://x402-analysis-gui.vercel.app"
                          (no trailing slash) -- PUBLIC_BASE_URL + /auth/callback
                          must be listed in the Auth0 application's Allowed
                          Callback URLs, and PUBLIC_BASE_URL itself in its
                          Allowed Logout URLs
    ALLOWED_EMAILS        required -- comma-separated list of email addresses
                          permitted to log in; anyone else who successfully
                          authenticates is still denied
"""

from __future__ import annotations

import logging
import os
import secrets
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/auth/callback"


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set.")
    return value


def _domain() -> str:
    return _required_env("AUTH0_DOMAIN").strip().rstrip("/")


def _redirect_uri() -> str:
    base = _required_env("PUBLIC_BASE_URL").rstrip("/")
    return f"{base}{CALLBACK_PATH}"


def new_state() -> str:
    """A random, unguessable token to protect the OAuth flow against CSRF."""
    return secrets.token_urlsafe(24)


def build_authorize_url(state: str) -> str:
    params = {
        "client_id": _required_env("AUTH0_CLIENT_ID"),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
    }
    return f"https://{_domain()}/authorize?{urlencode(params)}"


def build_logout_url() -> str:
    """Auth0's own logout endpoint -- ends the Auth0-side SSO session, not just
    our local cookie. Without this, clearing only our own session cookie would
    let a user get silently re-authenticated by Auth0 without re-entering
    credentials, since Auth0 keeps its own session too."""
    params = {
        "client_id": _required_env("AUTH0_CLIENT_ID"),
        "returnTo": _required_env("PUBLIC_BASE_URL").rstrip("/"),
    }
    return f"https://{_domain()}/v2/logout?{urlencode(params)}"


def exchange_code_for_email(code: str) -> str:
    """Exchange an authorization `code` for tokens and return the verified email address."""
    logger.info("oauth: exchanging authorization code for tokens")
    resp = requests.post(
        f"https://{_domain()}/oauth/token",
        data={
            "client_id": _required_env("AUTH0_CLIENT_ID"),
            "client_secret": _required_env("AUTH0_CLIENT_SECRET"),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": _redirect_uri(),
        },
        timeout=10,
    )
    resp.raise_for_status()
    access_token = resp.json()["access_token"]

    userinfo_resp = requests.get(
        f"https://{_domain()}/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    userinfo_resp.raise_for_status()
    userinfo = userinfo_resp.json()

    if not userinfo.get("email_verified"):
        raise RuntimeError(f"Auth0 account email is not verified: {userinfo.get('email')!r}")

    email = userinfo["email"]
    logger.info("oauth: exchanged code for email=%s", email)
    return email


def is_email_allowed(email: str) -> bool:
    allowed = {e.strip().lower() for e in os.getenv("ALLOWED_EMAILS", "").split(",") if e.strip()}
    return email.strip().lower() in allowed
