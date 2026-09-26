"""One row per test case, for people and for CI.

test.py records each suite's cases in a Report, which, when the suite
ends, writes framework/var/results/<suite>.tsv:

  suite<TAB>case<TAB>status<TAB>seconds<TAB>note

with that header row. `status` is PASS, FAIL, SKIP, or INCONCLUSIVE.
`seconds` is empty when a case has no duration of its own. A note's
backslashes, tabs, carriage returns, and newlines are written as \\\\,
\\t, \\r, and \\n, so each row is one line. `column -t -s "$(printf '\\t')"` lines the columns up.

The suite is the suite's name, with `-<tag>` after it if RESULTS_TAG
names a tag (as smoke.sh does, to tell apart two runs of one suite).
framework/report.py merges these files and converts them to JUnit XML
and Markdown.
"""

import os
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

PASS, FAIL, SKIP, INCONCLUSIVE = "PASS", "FAIL", "SKIP", "INCONCLUSIVE"
STATUSES = (PASS, FAIL, SKIP, INCONCLUSIVE)

COLUMNS = ("suite", "case", "status", "seconds", "note")

_ESCAPES = {"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"}
_UNESCAPES = {v: k for k, v in _ESCAPES.items()}


def escape(text: str) -> str:
    return "".join(_ESCAPES.get(c, c) for c in text)


def unescape(text: str) -> str:
    return re.sub(r"\\[\\tnr]", lambda m: _UNESCAPES[m.group(0)], text)


def format_table(columns: Sequence[str], rows: Sequence[Dict[str, str]]) -> str:
    """A header row, then each row's columns, tab-separated and escaped."""
    lines = ["\t".join(columns)]
    lines += ["\t".join(escape(row.get(c, "")) for c in columns) for row in rows]
    return "\n".join(lines) + "\n"


def parse_table(text: str) -> List[Dict[str, str]]:
    """The rows of a table that format_table wrote, by column name."""
    lines = [line for line in text.splitlines() if line]
    if not lines:
        return []
    columns = lines[0].split("\t")
    return [dict(zip(columns, (unescape(v) for v in line.split("\t")))) for line in lines[1:]]


def slug(name: str) -> str:
    """name, safe as a file name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "results"


@dataclass(frozen=True)
class Row:
    case: str
    status: str
    seconds: Optional[float]
    note: str


class Report:
    """The cases one suite ran. A case's duration is, by default, the
    time since the previous case, or since the Report was made."""

    def __init__(self, suite: str, directory: str):
        tag = os.environ.get("RESULTS_TAG")
        self.suite = f"{suite}-{tag}" if tag else suite
        self.path = os.path.join(directory, slug(self.suite) + ".tsv")
        self.rows: List[Row] = []
        self._mark = time.monotonic()

    def add(self, case: str, status: str, note: str = "",
            seconds: Optional[float] = -1.0) -> None:
        """Record a case. By default its duration is the time since the
        previous case; pass None for no duration."""
        if status not in STATUSES:
            raise ValueError(f"unknown status: {status}")
        now = time.monotonic()
        if seconds is not None and seconds < 0:
            seconds = now - self._mark
        self._mark = now
        self.rows.append(Row(case, status, seconds, note))

    @property
    def failed(self) -> int:
        return sum(1 for row in self.rows if row.status == FAIL)

    def count(self, status: str) -> int:
        return sum(1 for row in self.rows if row.status == status)

    def table(self) -> str:
        rows = [{"suite": self.suite, "case": r.case, "status": r.status,
                 "seconds": "" if r.seconds is None else f"{r.seconds:.1f}", "note": r.note}
                for r in self.rows]
        return format_table(COLUMNS, rows)

    def write(self, error: Optional[str] = None) -> None:
        """Write the file. error is why the suite stopped early, if it did:
        a FAIL row named `setup` if it recorded no case, or else
        `aborted`."""
        if error:
            self.add("aborted" if self.rows else "setup", FAIL, error)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            f.write(self.table())
