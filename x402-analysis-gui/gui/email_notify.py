"""Resend email notifications for x402-analysis-gui.

Used when someone completes the Auth0 signup flow (/auth/signup) with an
email that isn't yet in the user_settings allowlist: notifies a site admin
by email so they can decide whether to approve it (see scripts/add_user.py).
A failure here is logged, never raised -- a notification hiccup shouldn't
break the signup flow itself.

Env vars:
    RESEND_API_KEY       required to actually send -- from resend.com
    ADMIN_NOTIFY_EMAIL   required -- where the approval-request email goes
    RESEND_FROM_EMAIL    optional -- the "from" address; must be on a
                         domain verified with Resend to deliver broadly.
                         Defaults to Resend's shared sandbox address, which
                         only delivers to the Resend account's own verified
                         email -- fine for local testing, not for production.
"""

from __future__ import annotations

import logging
import os
from html import escape

import requests

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
_DEFAULT_FROM = "x402 Analysis <onboarding@resend.dev>"


def send_access_request(requester_email: str) -> bool:
    """Emails the admin (ADMIN_NOTIFY_EMAIL) asking them to approve
    `requester_email` for access. Returns True if Resend accepted the
    request, False on any failure (missing config, network error, or a
    non-2xx response -- all logged)."""
    api_key = os.getenv("RESEND_API_KEY")
    admin_email = os.getenv("ADMIN_NOTIFY_EMAIL")
    if not api_key or not admin_email:
        logger.warning(
            "email_notify: RESEND_API_KEY/ADMIN_NOTIFY_EMAIL not set; skipping "
            "access-request email for %s",
            requester_email,
        )
        return False

    from_address = os.getenv("RESEND_FROM_EMAIL", _DEFAULT_FROM)
    safe_email = escape(requester_email)
    payload = {
        "from": from_address,
        "to": [admin_email],
        "subject": f"x402 Analysis: access request from {requester_email}",
        "html": (
            f"<p>{safe_email} just signed up for x402 Analysis and is requesting access.</p>"
            "<p>Approve them with:</p>"
            f"<pre>python scripts/add_user.py {safe_email} --user-type standard</pre>"
        ),
    }

    try:
        resp = requests.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        logger.exception("email_notify: failed to send access-request email for %s", requester_email)
        return False

    logger.info("email_notify: sent access-request email for %s to %s", requester_email, admin_email)
    return True
