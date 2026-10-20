"""Writing counts as LaTeX tables (and as plain-text tables for the
terminal) -- shared by the 3x scripts that write dataset/*.tex.

A count table has a row per name and a column per (title, name -> count)
Counter, sorted by the first column, most first, with some rows (Unknown
and the like) held back to the bottom, then a Total row.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

LATEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def latex_escape(text: str) -> str:
    return "".join(LATEX_SPECIALS.get(ch, ch) for ch in text)


def ordered_rows(counts: list[Counter], last_rows: tuple[str, ...], order=None) -> list[str]:
    """Every name in `counts`, sorted by `order(name)` if given, otherwise
    by the first Counter, most first (ties by the next Counters, then
    name), with `last_rows` (those that have any count) after them in that
    order."""
    names = set().union(*counts)
    rows = sorted(names - set(last_rows), key=order or (lambda c: (*(-n[c] for n in counts), c)))
    return rows + [name for name in last_rows if name in names]


def latex_table(
    first_title: str,
    columns: list[tuple[str, Counter]],
    caption: str,
    label: str,
    source: str,
    last_rows: tuple[str, ...] = (),
    note: str | None = None,
    display=lambda name: name,
    order=None,
) -> str:
    """A booktabs table environment: a `first_title` column of names, then
    a column per (title, name -> count) in `columns`, rows ordered by
    ordered_rows (`order`: its sort key), then a Total row, then `note`
    (if any) in small type.
    `display(name)`: the text shown for a name. `source`: what wrote the
    table, for a comment at the top."""
    counts = [counter for _title, counter in columns]
    header = " & ".join([first_title, *(title for title, _counter in columns)])
    return "\n".join(
        [
            f"% Written by {source} -- don't edit by hand.",
            "% Needs \\usepackage{booktabs}.",
            "\\begin{table}[htbp]",
            "  \\centering",
            f"  \\caption{{{latex_escape(caption)}}}",
            f"  \\label{{{label}}}",
            "  \\begin{tabular}{l" + "r" * len(columns) + "}",
            "    \\toprule",
            f"    {header} \\\\",
            "    \\midrule",
            *(
                f"    {latex_escape(display(name))} & " + " & ".join(f"{n[name]:,}" for n in counts) + " \\\\"
                for name in ordered_rows(counts, last_rows, order)
            ),
            "    \\midrule",
            "    Total & " + " & ".join(f"{sum(n.values()):,}" for n in counts) + " \\\\",
            "    \\bottomrule",
            "  \\end{tabular}",
            *(["", "  \\smallskip", f"  \\footnotesize {note}"] if note else []),
            "\\end{table}",
            "",
        ]
    )


def text_table(
    first_title: str,
    columns: list[tuple[str, Counter]],
    last_rows: tuple[str, ...] = (),
    display=lambda name: name,
    order=None,
) -> str:
    """The same rows as latex_table, as an aligned plain-text table."""
    counts = [counter for _title, counter in columns]
    lines = [[first_title, *(title for title, _counter in columns)]]
    body = [[display(name), *(f"{n[name]:,}" for n in counts)] for name in ordered_rows(counts, last_rows, order)]
    total = ["Total", *(f"{sum(n.values()):,}" for n in counts)]
    widths = [max(len(row[i]) for row in [*lines, *body, total]) for i in range(len(total))]

    def fmt(row: list[str]) -> str:
        return "  ".join([row[0].ljust(widths[0]), *(cell.rjust(w) for cell, w in zip(row[1:], widths[1:]))])

    rule = "  ".join("-" * w for w in widths)
    return "\n".join([fmt(lines[0]), rule, *map(fmt, body), rule, fmt(total)])


def write_tex(path: Path, text: str) -> None:
    """Writes `text` via a temporary file renamed into place, so `path` is
    never left half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
