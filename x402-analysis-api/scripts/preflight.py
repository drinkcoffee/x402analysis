"""Shared preflight checks for update.py and the scripts it calls
(download_db.py, check_supported.py): required env vars and Neon
connectivity, checked up front with one aggregated, actionable report
instead of failing with a traceback -- or, worse, a partially-applied
change -- partway through a run.

Deliberately stdlib-only, so it's safe to import and use before even
knowing whether this project's own third-party dependencies (psycopg2,
etc.) are installed. Checking *those* isn't done here: Python can't
partially import a module, so a missing package is caught with a plain
try/except ImportError around each script's own import block instead (see
update.py/download_db.py/check_supported.py/load_facilitators.py), printing
which package is missing and how to install it, then exiting immediately --
there's nothing to usefully aggregate there the way there is for env vars,
external tools, and connectivity, which are genuinely independent of each
other and worth reporting together.

Usage, in any script's main():
    preflight.check_or_exit(
        some_module.preflight_checks,             # a module's own list[str]-returning check
        preflight.require_database_url(neon_url),
    )
"""

from __future__ import annotations

import os
import sys
from typing import Callable, Optional, Union

Check = Callable[[], Union[str, list, None]]


class PreflightError(Exception):
    """Raised by run() with every problem found, formatted for printing."""


def run(*checks: Check) -> None:
    """Calls every check (even after one reports a problem), so everything
    wrong is reported together in one PreflightError instead of one at a
    time across repeated runs. Each check returns a problem description
    (str), several (list[str]), or nothing wrong (None/empty)."""
    problems: list[str] = []
    for check in checks:
        result = check()
        if not result:
            continue
        problems.extend([result] if isinstance(result, str) else list(result))
    if problems:
        header = f"{len(problems)} problem(s) found before this could run:"
        raise PreflightError(header + "\n\n" + "\n\n".join(f"- {p}" for p in problems))


def check_or_exit(*checks: Check) -> None:
    """run(*checks), printing and exiting(1) instead of raising -- what
    every script's main() actually wants."""
    try:
        run(*checks)
    except PreflightError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)


def require_env(name: str, how_to_fix: str) -> Check:
    """A check(): confirms env var `name` is set (non-empty)."""

    def check() -> Optional[str]:
        if not os.getenv(name):
            return f"{name} is not set. {how_to_fix}"
        return None

    return check


def require_database_url(url: Optional[str], label: str = "DATABASE_URL") -> Check:
    """A check(): confirms `url` is set and something is actually
    listening and reachable at it. Needs psycopg2 to test connectivity --
    if it isn't installed, silently skips that part of the check (the
    script's own guarded `import psycopg2` already reports that
    separately, with its own clear message)."""

    def check() -> Optional[str]:
        if not url:
            return (
                f"{label} is not set. Add it to a .env file in this "
                "project's root (see .env.example), or export it -- it's a "
                "Postgres connection string from your Neon project's "
                "Connection Details in the Neon console."
            )
        try:
            import psycopg2
        except ImportError:
            return None
        try:
            psycopg2.connect(url, connect_timeout=10).close()
        except Exception as exc:  # noqa: BLE001
            return (
                f"couldn't connect to {label} ({exc}). Check the connection "
                "string is correct and current (Neon's pooled endpoint can "
                "change) and that the database is online."
            )
        return None

    return check
