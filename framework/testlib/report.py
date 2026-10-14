"""One row per test case, for people and for CI.

test.py records each suite's cases in a Report, which, when the suite
ends, writes framework/var/results/<suite>.tsv:

  suite<TAB>case<TAB>status<TAB>seconds<TAB>note

with that header row. `status` is PASS, FAIL, SKIP, or INCONCLUSIVE.
`seconds` is empty when a case has no duration of its own. A note's
backslashes, tabs, carriage returns, and newlines are written as \\\\,
\\t, \\r, and \\n, so each row is one line, and
`column -t -s "$(printf '\\t')"` lines the columns up.

The suite is the suite's name, with `-<tag>` after it if RESULTS_TAG
names a tag (as smoke.sh does, to tell apart two runs of one suite).
framework/report.py merges these files and converts them to JUnit XML
and Markdown.

A suite that prints a row per request records each with record() or
record_skip().
"""

import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Optional

from . import web

PASS, FAIL, SKIP, INCONCLUSIVE = "PASS", "FAIL", "SKIP", "INCONCLUSIVE"
STATUSES = (PASS, FAIL, SKIP, INCONCLUSIVE)

COLUMNS = ("suite", "case", "status", "seconds", "note")

_ESCAPES = {"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"}
_UNESCAPES = {v: k for k, v in _ESCAPES.items()}


def escape(text: str) -> str:
    """Return text, with each character that would break a row escaped."""
    return "".join(_ESCAPES.get(c, c) for c in text)


def unescape(text: str) -> str:
    """Return text, with what escape() did undone."""
    return re.sub(r"\\[\\tnr]", lambda m: _UNESCAPES[m.group(0)], text)


def format_table(columns: Sequence[str], rows: Sequence[dict[str, str]]) -> str:
    """Return a table: a header row, then each row's columns.

    The columns are tab-separated and escaped.
    """
    lines = ["\t".join(columns)]
    lines += ["\t".join(escape(row.get(c, "")) for c in columns) for row in rows]
    return "\n".join(lines) + "\n"


def parse_table(text: str) -> list[dict[str, str]]:
    """Return the rows of a table that format_table wrote, by column name.

    format_table ends each row with a newline, and escapes any other, so
    rows are split at newlines alone (not at the other line boundaries
    that str.splitlines() knows).

    Raises:
        ValueError: A row's number of fields differs from the header's.
    """
    lines = [line for line in text.split("\n") if line]
    if not lines:
        return []
    columns = lines[0].split("\t")
    rows: list[dict[str, str]] = []
    for n, line in enumerate(lines[1:], start=2):
        values = line.split("\t")
        if len(values) != len(columns):
            raise ValueError(f"row {n} has {len(values)} fields, not {len(columns)}")
        rows.append(dict(zip(columns, (unescape(v) for v in values))))
    return rows


def slug(name: str) -> str:
    """Return name, safe as a file name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "results"


@dataclass(frozen=True)
class Row:
    """A case, as recorded.

    Attributes:
        status: One of STATUSES.
        seconds: Its duration, or None if it has none of its own.
    """

    case: str
    status: str
    seconds: Optional[float]
    note: str


class Report:
    """The cases one suite ran.

    A case's duration is, by default, the time since the previous case,
    or since the Report was made.

    Attributes:
        suite: The suite's name, with `-<tag>` after it if RESULTS_TAG
            names a tag.
        path: The file that write() writes.
    """

    def __init__(self, suite: str, directory: str):
        tag = os.environ.get("RESULTS_TAG")
        self.suite = f"{suite}-{tag}" if tag else suite
        self.path = os.path.join(directory, slug(self.suite) + ".tsv")
        self.rows: list[Row] = []
        self._mark = time.monotonic()

    def add(
        self, case: str, status: str, note: str = "", seconds: Optional[float] = -1.0
    ) -> None:
        """Record a case.

        By default its duration is the time since the previous case;
        pass None for no duration.
        """
        if status not in STATUSES:
            raise ValueError(f"unknown status: {status}")
        now = time.monotonic()
        if seconds is not None and seconds < 0:
            seconds = now - self._mark
        self._mark = now
        self.rows.append(Row(case, status, seconds, note))

    @property
    def failed(self) -> int:
        """How many cases failed."""
        return sum(1 for row in self.rows if row.status == FAIL)

    def count(self, status: str) -> int:
        """Return how many cases have status."""
        return sum(1 for row in self.rows if row.status == status)

    def table(self) -> str:
        """Return the cases, as a table."""
        rows = [
            {
                "suite": self.suite,
                "case": r.case,
                "status": r.status,
                "seconds": "" if r.seconds is None else f"{r.seconds:.1f}",
                "note": r.note,
            }
            for r in self.rows
        ]
        return format_table(COLUMNS, rows)

    def write(self, error: Optional[str] = None) -> None:
        """Write the file.

        error is why the suite stopped early, if it did: it is recorded
        as a FAIL row named `setup` if the suite recorded no case, or
        else `aborted`.
        """
        if error:
            self.add("aborted" if self.rows else "setup", FAIL, error)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(self.table())


def label(svc: str, role: str) -> str:
    """Return what rows call service svc in role (origin or cache).

    In `tiny`, one service is both.
    """
    return svc if svc.startswith(f"{role}-") else f"{svc}-{role}"


def record(
    report: Report, heading: str, case: str, answer: web.Response, why: str, note: str = ""
) -> None:
    """Print heading and answer's status as a row, and record case.

    It failed because of why, if there is one, or else passed, with
    note.
    """
    code = "000" if answer.status is None else str(answer.status)
    line = f"{heading} {code:>4}"
    if why:
        print(f"{line}  FAIL  {why}")
        report.add(case, FAIL, f"HTTP {code}: {why}")
    else:
        print(f"{line}  PASS" + (f"  {note}" if note else ""))
        report.add(case, PASS, f"HTTP {code}" + (f": {note}" if note else ""))


def record_skip(report: Report, heading: str, case: str, why: str) -> None:
    """Print heading as a row, and record case, skipped because of why."""
    print(f"{heading}       SKIP  {why}")
    report.add(case, SKIP, why)
