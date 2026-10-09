"""Code shared by gather's numbered scripts. Everything here is either
written for gather or copied from elsewhere in this repo (each copied
module says where from), so gather doesn't depend on any other directory."""

from pathlib import Path

GATHER_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = GATHER_ROOT / "dataset"
