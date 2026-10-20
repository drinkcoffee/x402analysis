#!/usr/bin/env python3
"""Splits dataset/funders.json (written by 07_find_funders_base.py) into
two pieces small enough for GitHub, which refuses files over 100 MB, and
restores funders.json from them.

    split:   dataset/funders.json -> dataset/funders.part1.json,
                                     dataset/funders.part2.json
    restore: the two pieces -> dataset/funders.json

Each piece is a valid JSON file on its own: funders.json's other fields
(gathered_at, chain, failed, incomplete, ...) and half of its "funders"
list (cut so the two pieces are about the same size), plus a "split"
field saying which piece it is, the order of funders.json's fields, and
the SHA-256 and size of funders.json. Restore
joins the two halves, writes the result the way 07 writes it
(common.write_json), and checks it against that SHA-256, so the restored
file is byte-for-byte the original. Split checks first that funders.json
is in that format, so that restore can rebuild it exactly.

dataset/funders.json is in .gitignore; commit the pieces. Split never
deletes funders.json, and overwrites the pieces. Restore won't overwrite
a funders.json that differs from the pieces' unless given --force.

Doesn't use the database.

Usage:
    python gather/40_split_funders_file.py split
    python gather/40_split_funders_file.py restore [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

GATHER_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(GATHER_ROOT))

from gatherlib import DATASET_DIR  # noqa: E402
from gatherlib.common import write_json  # noqa: E402

FULL_PATH = DATASET_DIR / "funders.json"
PIECE_PATHS = [DATASET_DIR / f"funders.part{n}.json" for n in (1, 2)]
SPLIT_LIST = "funders"
GITHUB_LIMIT = 100 * 1024 * 1024


def serialise(data) -> bytes:
    """`data` exactly as common.write_json writes it."""
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()


def rel(path: Path) -> Path:
    return path.relative_to(GATHER_ROOT)


def halfway(items: list) -> int:
    """Where to cut `items` so the two halves are about the same size as
    JSON (entries vary a lot in size, so half the count isn't half the
    bytes)."""
    sizes = [len(json.dumps(item, indent=2, ensure_ascii=False).encode()) for item in items]
    target, total = sum(sizes) / 2, 0
    for index, size in enumerate(sizes):
        total += size
        if total >= target:
            return index + 1
    return len(items)


def split() -> None:
    if not FULL_PATH.exists():
        sys.exit(f"{rel(FULL_PATH)} doesn't exist -- run 07_find_funders_base.py, or restore it from the pieces.")
    raw = FULL_PATH.read_bytes()
    data = json.loads(raw)
    if serialise(data) != raw:
        sys.exit(
            f"{rel(FULL_PATH)} isn't formatted the way 07 writes it, so it couldn't be restored "
            "byte-for-byte; not splitting."
        )
    funders = data[SPLIT_LIST]
    half = halfway(funders)
    halves = [funders[:half], funders[half:]]
    meta = {
        "of": FULL_PATH.name,
        "parts": len(PIECE_PATHS),
        "list": SPLIT_LIST,
        "list_count": len(funders),
        "keys": list(data),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }
    for n, (path, items) in enumerate(zip(PIECE_PATHS, halves), start=1):
        piece = {"split": {**meta, "part": n}}
        piece.update((key, items if key == SPLIT_LIST else value) for key, value in data.items())
        write_json(path, piece)
        size = path.stat().st_size
        warning = "  -- over GitHub's 100 MB limit!" if size > GITHUB_LIMIT else ""
        print(f"wrote {rel(path)}: {len(items):,} funders, {size / 1e6:,.1f} MB{warning}")
    print(f"split {rel(FULL_PATH)} ({len(raw) / 1e6:,.1f} MB, {len(funders):,} funders); it's left in place.")


def restore(force: bool) -> None:
    missing = [rel(path) for path in PIECE_PATHS if not path.exists()]
    if missing:
        sys.exit(f"missing: {', '.join(map(str, missing))} -- nothing to restore from.")
    pieces = [json.loads(path.read_bytes()) for path in PIECE_PATHS]
    metas = [piece.pop("split", None) for piece in pieces]
    if any(meta is None for meta in metas):
        sys.exit("a piece has no \"split\" field -- not written by this script.")
    meta = metas[0]
    if any(
        {k: v for k, v in m.items() if k != "part"} != {k: v for k, v in meta.items() if k != "part"} for m in metas
    ) or [m["part"] for m in metas] != list(range(1, len(PIECE_PATHS) + 1)):
        sys.exit("the pieces are from different splits (re-run split).")
    items = [item for piece in pieces for item in piece[meta["list"]]]
    if len(items) != meta["list_count"]:
        sys.exit(f"the pieces have {len(items):,} {meta['list']}, not {meta['list_count']:,}.")
    data = {key: items if key == meta["list"] else pieces[0][key] for key in meta["keys"]}
    raw = serialise(data)
    if hashlib.sha256(raw).hexdigest() != meta["sha256"]:
        sys.exit(f"the restored file doesn't match {meta['of']}'s SHA-256 -- not writing it.")
    if FULL_PATH.exists():
        if hashlib.sha256(FULL_PATH.read_bytes()).hexdigest() == meta["sha256"]:
            print(f"{rel(FULL_PATH)} already matches the pieces; nothing to do.")
            return
        if not force:
            sys.exit(f"{rel(FULL_PATH)} exists and differs from the pieces; use --force to overwrite it.")
    write_json(FULL_PATH, data)
    print(f"restored {rel(FULL_PATH)} ({len(raw) / 1e6:,.1f} MB, {len(items):,} funders); SHA-256 matches.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["split", "restore"])
    parser.add_argument("--force", action="store_true", help="restore: overwrite a funders.json that differs")
    args = parser.parse_args()
    if args.action == "split":
        split()
    else:
        restore(args.force)


if __name__ == "__main__":
    main()
