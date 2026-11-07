#!/usr/bin/env -S uv run --quiet --frozen --no-dev
"""Merge the tests' results and convert them for CI.

  framework/report.py merge [NAME=]DIR...  one table from each DIR's *.tsv
  framework/report.py combine OUT DIR...   OUT/*.tsv from DIRs of one shape
  framework/report.py junit TABLE          JUnit XML
  framework/report.py markdown TABLE       a Markdown summary

Each suite writes framework/var/results/<suite>.tsv (see
framework/testlib/report.py). `merge` prefixes each row with a `shape`
column: NAME, or DIR's own name. smoke.sh keeps one DIR per shape and
merges them into results.tsv, then converts that. Output goes to stdout,
but combine's, which goes to OUT.

`combine` makes one run's results of a shape that several runs, each
against a federation of its own, shared out (`smoke.sh -j`): each table
holds each DIR's rows of it, each case once. A case that more than one
run recorded (federation's REQUIRED scenarios, which each runs, and
smoke.sh's own steps) keeps its worst status.

The TSV is meant for people as much as for programs:

  column -t -s "$(printf '\\t')" results.tsv
"""

import os
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence

# Keep the checkout free of __pycache__.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# pylint: disable=wrong-import-position
from testlib import common, report

# pylint: enable=wrong-import-position

MERGED = ("shape",) + report.COLUMNS


def merge(args: Sequence[str]) -> str:
    """Return one table of every DIR's results, each row with its shape.

    Args:
        args: Each `[NAME=]DIR`: a directory of *.tsv tables, and the
            shape of their rows (by default, DIR's own name).
    """
    rows: list[dict[str, str]] = []
    for arg in args:
        name, sep, directory = arg.partition("=")
        if not sep:
            name, directory = os.path.basename(os.path.normpath(arg)), arg
        if not os.path.isdir(directory):
            common.die_usage(f"no such directory: {directory}")
        for entry in sorted(os.listdir(directory)):
            if not entry.endswith(".tsv"):
                continue
            with open(os.path.join(directory, entry), encoding="utf-8") as f:
                for row in report.parse_table(f.read()):
                    rows.append({"shape": name, **row})
    return report.format_table(MERGED, rows)


# Which of a case's statuses combine() keeps: the first of these.
WORST_FIRST = (report.FAIL, report.INCONCLUSIVE, report.PASS, report.SKIP)


def rank(row: dict[str, str]) -> int:
    """Return where row's status is in WORST_FIRST (unknown ones first)."""
    status = row.get("status", "")
    return WORST_FIRST.index(status) if status in WORST_FIRST else -1


def combine(out: str, directories: Sequence[str]) -> None:
    """Write out/<table> for each *.tsv table in any of directories.

    Each holds the rows of every directory's table of that name, in the
    order first seen, each (suite, case) once, with its worst status.
    """
    tables: dict[str, dict[tuple[str, str], dict[str, str]]] = {}
    columns: dict[str, Sequence[str]] = {}
    for directory in directories:
        if not os.path.isdir(directory):
            common.die_usage(f"no such directory: {directory}")
        for entry in sorted(os.listdir(directory)):
            if not entry.endswith(".tsv"):
                continue
            with open(os.path.join(directory, entry), encoding="utf-8") as f:
                text = f.read()
            columns.setdefault(entry, text.split("\n", 1)[0].split("\t"))
            rows = tables.setdefault(entry, {})
            for row in report.parse_table(text):
                key = (row.get("suite", ""), row.get("case", ""))
                if key not in rows or rank(row) < rank(rows[key]):
                    rows[key] = row
    os.makedirs(out, exist_ok=True)
    for entry, rows in tables.items():
        with open(os.path.join(out, entry), "w", encoding="utf-8") as f:
            f.write(report.format_table(columns[entry], list(rows.values())))


def load(path: str) -> list[dict[str, str]]:
    """Return the rows of the table at path, each with a shape."""
    with open(path, encoding="utf-8") as f:
        rows = report.parse_table(f.read())
    for row in rows:
        row.setdefault("shape", "")
    return rows


def groups(rows: Sequence[dict[str, str]]) -> dict[tuple[str, str], list[dict[str, str]]]:
    """Return rows by (shape, suite), in the order they first appear."""
    found: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        found.setdefault((row.get("shape", ""), row.get("suite", "")), []).append(row)
    return found


def seconds(row: dict[str, str]) -> float:
    """Return how long row's case took, or 0 if it does not say."""
    try:
        return float(row.get("seconds") or 0)
    except ValueError:
        return 0.0


# Characters that XML 1.0 forbids, which a note may hold (e.g. a
# terminal escape from a command's output).
_NOT_XML = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def xml_text(text: str) -> str:
    """Return text, writing each character that XML 1.0 forbids as \\xNN."""
    return _NOT_XML.sub(lambda m: f"\\x{ord(m.group(0)):02x}", text)


def count(rows: Sequence[dict[str, str]], status: str) -> int:
    """Return how many of rows have status."""
    return sum(1 for row in rows if row.get("status") == status)


def junit(rows: Sequence[dict[str, str]]) -> str:
    """Return JUnit XML of rows: a <testsuite> per shape and suite.

    FAIL is a <failure>; SKIP and INCONCLUSIVE are <skipped>, the latter's
    message saying so.
    """
    top = ET.Element("testsuites", name="pelican-test-framework")
    totals = {"tests": 0, "failures": 0, "skipped": 0, "time": 0.0}
    for (shape, suite), members in groups(rows).items():
        name = f"{shape}/{suite}" if shape else suite
        failures = count(members, report.FAIL)
        skipped = count(members, report.SKIP) + count(members, report.INCONCLUSIVE)
        took = sum(seconds(r) for r in members)
        element = ET.SubElement(
            top,
            "testsuite",
            name=xml_text(name),
            tests=str(len(members)),
            failures=str(failures),
            errors="0",
            skipped=str(skipped),
            time=f"{took:.1f}",
        )
        for row in members:
            case = ET.SubElement(
                element,
                "testcase",
                name=xml_text(row.get("case", "")),
                classname=xml_text(name.replace("/", ".")),
                time=f"{seconds(row):.1f}",
            )
            status, note = row.get("status"), xml_text(row.get("note", ""))
            if status == report.FAIL:
                ET.SubElement(case, "failure", message=note)
            elif status == report.SKIP:
                ET.SubElement(case, "skipped", message=note)
            elif status == report.INCONCLUSIVE:
                ET.SubElement(case, "skipped", message=f"inconclusive: {note}")
            elif note:
                ET.SubElement(case, "system-out").text = note
        totals["tests"] += len(members)
        totals["failures"] += failures
        totals["skipped"] += skipped
        totals["time"] += took
    for key, value in totals.items():
        top.set(key, f"{value:.1f}" if key == "time" else str(value))
    ET.indent(top)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(top, encoding="unicode") + "\n"


def cell(text: str) -> str:
    """Return text, safe in a Markdown table's cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def markdown(rows: Sequence[dict[str, str]]) -> str:
    """Return a table of counts per shape and suite, then every failure."""
    lines = ["## Pelican smoke tests", ""]
    failed = count(rows, report.FAIL)
    lines.append(f"{len(rows)} cases: {len(rows) - failed} did not fail, {failed} failed.")
    lines += [
        "",
        "| Shape | Suite | Pass | Fail | Skip | Inconclusive |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for (shape, suite), members in groups(rows).items():
        lines.append(
            f"| {cell(shape)} | {cell(suite)} | {count(members, report.PASS)}"
            + f" | {count(members, report.FAIL)} | {count(members, report.SKIP)}"
            + f" | {count(members, report.INCONCLUSIVE)} |"
        )
    failures = [r for r in rows if r.get("status") == report.FAIL]
    if failures:
        lines += ["", "### Failures", ""]
        for row in failures:
            where = " / ".join(x for x in (row.get("shape"), row.get("suite"), row.get("case")) if x)
            lines.append(f"- **{cell(where)}**: {cell(row.get('note', ''))}")
    return "\n".join(lines) + "\n"


def main() -> None:
    """Run the command that the arguments name."""
    args = sys.argv[1:]
    if args[:1] in ([], ["-h"], ["--help"]):
        print((__doc__ or "").strip("\n"))
        sys.exit(0 if args else 2)
    command, rest = args[0], args[1:]
    if command == "merge":
        if not rest:
            common.die_usage("merge needs at least one directory")
        sys.stdout.write(merge(rest))
    elif command == "combine":
        if len(rest) < 2:
            common.die_usage("combine needs OUT and at least one directory")
        combine(rest[0], rest[1:])
    elif command in ("junit", "markdown"):
        if len(rest) != 1:
            common.die_usage(f"{command} takes one table")
        rows = load(rest[0])
        sys.stdout.write(junit(rows) if command == "junit" else markdown(rows))
    else:
        common.die_usage(f"unknown command: {command} (try: merge, combine, junit, markdown)")


if __name__ == "__main__":
    main()
