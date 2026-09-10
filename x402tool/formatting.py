"""Output helpers: plain tables and response pretty-printing."""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable, Sequence

import requests


def print_table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
    rows = [tuple(str(c) for c in row) for row in rows]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: Sequence[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    print(fmt_row(headers))
    print(fmt_row(["-" * w for w in widths]))
    for row in rows:
        print(fmt_row(row))


def response_body(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def print_response(response: requests.Response, as_json: bool, label: str | None = None) -> int:
    """Print a requests.Response, return a process exit code (0 on 2xx)."""
    body = response_body(response)
    if as_json:
        payload = {"status": response.status_code, "body": body}
        print(json.dumps(payload, indent=2, default=str))
    else:
        header = f"{label + ' - ' if label else ''}HTTP {response.status_code}"
        print(header, file=sys.stderr)
        if isinstance(body, (dict, list)):
            print(json.dumps(body, indent=2, default=str))
        else:
            print(body)
    return 0 if response.ok else 1


def print_error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1
