#!/usr/bin/env python3
"""Adds, updates, removes, or lists authorised users in user_settings.

The running web app has no self-service "invite a user" UI -- this is how
you seed the very first Admin user, and how you manage the allowlist after
that.

Usage:
    python scripts/add_user.py you@example.com --user-type admin
    python scripts/add_user.py someone@example.com --user-type standard --light-mode dark
    python scripts/add_user.py someone@example.com --remove
    python scripts/add_user.py --list

Requires DATABASE_URL and DB_ENCRYPTION_KEY, read from a .env file in the
project root (if present) or the real environment.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from gui.db import (  # noqa: E402
    LIGHT_MODE_AUTO,
    LIGHT_MODE_DARK,
    LIGHT_MODE_LIGHT,
    USER_TYPE_ADMIN,
    USER_TYPE_ADVANCED,
    USER_TYPE_NAMES,
    USER_TYPE_STANDARD,
    add_user,
    list_users,
    remove_user,
)

USER_TYPES = {"admin": USER_TYPE_ADMIN, "advanced": USER_TYPE_ADVANCED, "standard": USER_TYPE_STANDARD}
LIGHT_MODES = {"auto": LIGHT_MODE_AUTO, "light": LIGHT_MODE_LIGHT, "dark": LIGHT_MODE_DARK}
LIGHT_MODE_NAMES = {v: k for k, v in LIGHT_MODES.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("email", nargs="?", help="email address to add/update/remove")
    parser.add_argument("--user-type", choices=sorted(USER_TYPES), default="standard")
    parser.add_argument("--light-mode", choices=sorted(LIGHT_MODES), default="auto")
    parser.add_argument("--remove", action="store_true", help="remove this email instead of adding/updating it")
    parser.add_argument("--list", action="store_true", help="list all authorised users and exit")
    args = parser.parse_args()

    if args.list:
        users = list_users()
        if not users:
            print("(no authorised users yet)")
            return
        for user in users:
            print(
                f"{user['email']}\tuser_type={USER_TYPE_NAMES[user['user_type']]}"
                f"\tlight_mode={LIGHT_MODE_NAMES[user['light_mode']]}"
            )
        return

    if not args.email:
        parser.error("email is required unless --list is given")

    if args.remove:
        removed = remove_user(args.email)
        print(f"{args.email} -> {'removed' if removed else 'was not present'}")
        return

    add_user(args.email, user_type=USER_TYPES[args.user_type], light_mode=LIGHT_MODES[args.light_mode])
    print(f"{args.email} -> user_type={args.user_type}, light_mode={args.light_mode}")


if __name__ == "__main__":
    main()
