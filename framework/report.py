#!/usr/bin/env python3
"""Merge the tests' results and convert them for CI.

  framework/report.py merge [NAME=]DIR...  one table from each DIR's *.tsv
  framework/report.py junit TABLE          JUnit XML
  framework/report.py markdown TABLE       a Markdown summary

Each suite writes framework/var/results/<suite>.tsv (see
framework/testlib/report.py). `merge` prefixes each row with a `shape`
column: NAME, or DIR's own name. smoke.sh keeps one DIR per shape and
merges them into results.tsv, then converts that. Output goes to stdout.
The TSV is meant for people as much as for programs:

  column -t -s "$(printf '\\t')" results.tsv
"""

import sys

sys.dont_write_bytecode = True

import os  # noqa: E402
import re  # noqa: E402
import xml.etree.ElementTree as ET  # noqa: E402
from collections import OrderedDict  # noqa: E402
from typing import Dict, List, Sequence, Tuple  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from testlib import report  # noqa: E402

MERGED = ("shape",) + report.COLUMNS


def die_usage(message: str) -> None:
    print(f"report.py: {message}", file=sys.stderr)
    sys.exit(2)


def merge(args: Sequence[str]) -> str:
    rows: List[Dict[str, str]] = []
    for arg in args:
        name, sep, directory = arg.partition("=")
        if not sep:
            name, directory = os.path.basename(os.path.normpath(arg)), arg
        if not os.path.isdir(directory):
            die_usage(f"no such directory: {directory}")
        for entry in sorted(os.listdir(directory)):
            if not entry.endswith(".tsv"):
                continue
            with open(os.path.join(directory, entry)) as f:
                for row in report.parse_table(f.read()):
                    rows.append({"shape": name, **row})
    return report.format_table(MERGED, rows)


def load(path: str) -> List[Dict[str, str]]:
    with open(path) as f:
        rows = report.parse_table(f.read())
    for row in rows:
        row.setdefault("shape", "")
    return rows


def groups(rows: Sequence[Dict[str, str]]) -> "OrderedDict[Tuple[str, str], List[Dict[str, str]]]":
    """Rows by (shape, suite), in the order they first appear."""
    found: "OrderedDict[Tuple[str, str], List[Dict[str, str]]]" = OrderedDict()
    for row in rows:
        found.setdefault((row.get("shape", ""), row.get("suite", "")), []).append(row)
    return found


def seconds(row: Dict[str, str]) -> float:
    try:
        return float(row.get("seconds") or 0)
    except ValueError:
        return 0.0


# Characters that XML 1.0 forbids, which a note may hold (e.g. a
# terminal escape from a command's output).
_NOT_XML = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def xml_text(text: str) -> str:
    """text, with each character that XML 1.0 forbids written as \\xNN."""
    return _NOT_XML.sub(lambda m: f"\\x{ord(m.group(0)):02x}", text)


def count(rows: Sequence[Dict[str, str]], status: str) -> int:
    return sum(1 for row in rows if row.get("status") == status)


def junit(rows: Sequence[Dict[str, str]]) -> str:
    """A <testsuite> per shape and suite. FAIL is a <failure>; SKIP and
    INCONCLUSIVE are <skipped>, the latter's message saying so."""
    top = ET.Element("testsuites", name="pelican-test-framework")
    totals = {"tests": 0, "failures": 0, "skipped": 0, "time": 0.0}
    for (shape, suite), members in groups(rows).items():
        name = f"{shape}/{suite}" if shape else suite
        failures = count(members, report.FAIL)
        skipped = count(members, report.SKIP) + count(members, report.INCONCLUSIVE)
        took = sum(seconds(r) for r in members)
        element = ET.SubElement(top, "testsuite", name=xml_text(name), tests=str(len(members)),
                                failures=str(failures), errors="0", skipped=str(skipped),
                                time=f"{took:.1f}")
        for row in members:
            case = ET.SubElement(element, "testcase", name=xml_text(row.get("case", "")),
                                 classname=xml_text(name.replace("/", ".")),
                                 time=f"{seconds(row):.1f}")
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
    return text.replace("|", "\\|").replace("\n", " ")


def markdown(rows: Sequence[Dict[str, str]]) -> str:
    """A table of counts per shape and suite, then every failure."""
    lines = ["## Pelican smoke tests", ""]
    failed = count(rows, report.FAIL)
    lines.append(f"{len(rows)} cases: {len(rows) - failed} did not fail, {failed} failed.")
    lines += ["", "| Shape | Suite | Pass | Fail | Skip | Inconclusive |",
              "| --- | --- | ---: | ---: | ---: | ---: |"]
    for (shape, suite), members in groups(rows).items():
        lines.append(f"| {cell(shape)} | {cell(suite)} | {count(members, report.PASS)}"
                     f" | {count(members, report.FAIL)} | {count(members, report.SKIP)}"
                     f" | {count(members, report.INCONCLUSIVE)} |")
    failures = [r for r in rows if r.get("status") == report.FAIL]
    if failures:
        lines += ["", "### Failures", ""]
        for row in failures:
            where = " / ".join(x for x in (row.get("shape"), row.get("suite"), row.get("case")) if x)
            lines.append(f"- **{cell(where)}**: {cell(row.get('note', ''))}")
    return "\n".join(lines) + "\n"


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print((__doc__ or "").strip("\n"))
        sys.exit(0 if args else 2)
    command, rest = args[0], args[1:]
    if command == "merge":
        if not rest:
            die_usage("merge needs at least one directory")
        sys.stdout.write(merge(rest))
    elif command in ("junit", "markdown"):
        if len(rest) != 1:
            die_usage(f"{command} takes one table")
        rows = load(rest[0])
        sys.stdout.write(junit(rows) if command == "junit" else markdown(rows))
    else:
        die_usage(f"unknown command: {command} (try: merge, junit, markdown)")


if __name__ == "__main__":
    main()
