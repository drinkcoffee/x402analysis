"""A record of the work a long-running script has finished, so that if it
stops part way -- an outage, Ctrl-C, a crash -- running it again carries
on from where it got to instead of starting over.

One JSON object per line, appended (and flushed to disk) as each piece of
work finishes, in gather/localdb/progress/<name>.jsonl -- under localdb/,
so it's gitignored. A line cut short by a crash is dropped when the file's
read back.
"""

from __future__ import annotations

import json
import os

from . import GATHER_ROOT

PROGRESS_DIR = GATHER_ROOT / "localdb" / "progress"


class ProgressLog:
    def __init__(self, name: str) -> None:
        self.path = PROGRESS_DIR / f"{name}.jsonl"

    def load(self) -> list[dict]:
        """Every record saved so far ([] if none). Trims a last line left
        incomplete by a crash, so later records start on a line of their
        own."""
        if not self.path.exists():
            return []
        data = self.path.read_bytes()
        if data and not data.endswith(b"\n"):
            data = data[: data.rfind(b"\n") + 1]
            self.path.write_bytes(data)
        records = []
        for line in data.splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                pass
        return records

    def append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)
