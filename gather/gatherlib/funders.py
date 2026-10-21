"""Reading dataset/funders.json (written by 07_find_funders_base.py) -- or,
if it isn't there, the funders.part*.json pieces 40_split_funders_file.py
splits it into, since funders.json itself is too big to commit.
"""

from __future__ import annotations

import json
import sys

from . import DATASET_DIR

FUNDERS_PATH = DATASET_DIR / "funders.json"
FUNDERS_PIECES = "funders.part*.json"


def load_funders() -> list[dict]:
    """funders.json's "funders" list, or the pieces' joined; exits if
    neither is there."""
    if FUNDERS_PATH.exists():
        return json.loads(FUNDERS_PATH.read_text())["funders"]
    pieces = sorted(DATASET_DIR.glob(FUNDERS_PIECES))
    if not pieces:
        sys.exit(f"neither dataset/{FUNDERS_PATH.name} nor its pieces exist -- run 07_find_funders_base.py first.")
    return [funder for piece in pieces for funder in json.loads(piece.read_text())["funders"]]
