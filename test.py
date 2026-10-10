#!/usr/bin/env python3
"""Run the basic functionality tests against the running federation:
every suite that applies to its shape, over every combination of
client, route, namespace, credential, and server that the shape has.
Exits non-zero if any case fails.

  ./test.py                          everything that applies
  ./test.py auth commands            only these suites
  ./test.py 'transfers/plugin-*'     only the scenarios of a suite that match
  ./test.py -l [SUITE]...            list the suites and their scenarios

Run it in the dev container, e.g. from the host:

  ./fed.sh test [ARG]...

The suites are in framework/suites/, and run in this order: federation,
commands, transfer-api, listings, names, blocks, tiering, posc,
metadata, users, owners, sitelocal, auth, and transfers.
`federation/ready` and `federation/caches` run first whatever is asked
for, and if the federation never serves, nothing else runs. A suite
that doesn't apply to the shape, such as posc without POSC or pstore, is
skipped. Each suite writes a row per case to
framework/var/results/<suite>.tsv; a run of everything first removes
the results of earlier runs with the same RESULTS_TAG.
"""

import contextlib
import os
import sys
import tempfile
import time
import traceback
from typing import Optional

# No __pycache__ in the checkout, where the dev container would leave it
# root's.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "framework"))

# pylint: disable=wrong-import-position
from suites import SUITES
from testlib import common
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP, Report
from testlib.session import Session

# pylint: enable=wrong-import-position

RESULTS = os.path.join(common.VAR, "results")


def current_shape() -> Optional[common.Federation]:
    """The federation's shape, if fed.sh has written it (see
    common.Federation); None if not."""
    if not os.path.isfile(os.path.join(common.VAR, "generated", "origins")):
        return None
    common.enter_var()
    return common.Federation()


def skip_reason(name: str, fed: common.Federation) -> Optional[str]:
    """Why suite name skips in fed's shape, or None."""
    try:
        return SUITES[name].skip(fed)
    except common.Died:
        return None


def list_suites(chosen: dict[str, list[str]]) -> None:
    """Print the chosen suites and scenarios, and which skip."""
    fed = current_shape()
    for name, scenarios in chosen.items():
        module = SUITES[name]
        why = skip_reason(name, fed) if fed else None
        print(name + (f" (skipped in this shape: {why})" if why else ""))
        width = max(len(n) for n in module.SCENARIOS)
        required = getattr(module, "REQUIRED", ())
        for scenario in scenarios:
            always = " (always runs)" if scenario in required else ""
            print(f"  {scenario:<{width}} {module.SCENARIOS[scenario]}{always}")
        print()
    if fed is None:
        print("Run ./fed.sh init to see which suites apply to the shape.")


def with_required(chosen: dict[str, list[str]]) -> dict[str, list[str]]:
    """chosen, and each suite's REQUIRED scenarios, in the suites' order."""
    found: dict[str, list[str]] = {}
    for name, module in SUITES.items():
        required = getattr(module, "REQUIRED", ())
        if name in chosen or required:
            wanted = set(chosen.get(name, ())) | set(required)
            found[name] = [n for n in module.SCENARIOS if n in wanted]
    return found


def describe(fed: common.Federation) -> str:
    """fed's shape, briefly."""
    caches = "no cache"
    if fed.caches:
        kinds = ", ".join(sorted({c.kind for c in fed.caches}))
        caches = f"{len(fed.caches)} cache(s) ({kinds})"
    return (
        f"{fed.topology} topology: {len(fed.origins)} origin(s) ({fed.origin_variant}),"
        f" {caches}, {len(fed.directors)} director(s);"
        f" exporting {', '.join('/' + ns for ns in fed.exports)}"
    )


def run_suite(name: str, selected: list[str], session: Session) -> tuple[Report, bool]:
    """Run one suite's selected scenarios, and write its results: its
    report, and whether it finished. If it didn't, its report says why."""
    module = SUITES[name]
    report = Report(name, RESULTS)
    results = common.Results(list(module.SCENARIOS), report)
    print(f"\n==> {name}\n")
    error: Optional[str] = None
    try:
        why = module.skip(session.fed)
        if why:
            print(f"Skipping every scenario: {why}")
            for scenario in selected:
                report.add(scenario, SKIP, why, seconds=None)
        else:
            module.run(session, selected, results)
    except common.Died as e:
        error = e.message
    except KeyboardInterrupt:
        report.write("interrupted")
        raise
    except Exception as e:  # pylint: disable=broad-exception-caught
        # A bug, or a server answering what the suite can't read: the
        # suite fails, and the rest still run.
        traceback.print_exc()
        error = f"{type(e).__name__}: {e}"
    results.print()
    report.write(error)
    return report, error is None


def summarize(reports: list[Report], seconds: float) -> None:
    """Print each suite's counts, then every failed or unsure case."""
    width = max(len("suite"), *(len(r.suite) for r in reports))
    print(
        f"\n==> summary\n\n  {'suite':<{width}} {'pass':>5} {'fail':>5} {'skip':>5}"
        + f" {'inconclusive':>12}"
    )
    for r in reports:
        print(
            f"  {r.suite:<{width}} {r.count(PASS):>5} {r.failed:>5} {r.count(SKIP):>5}"
            + f" {r.count(INCONCLUSIVE):>12}"
        )
    for status in (FAIL, INCONCLUSIVE):
        rows = [(r.suite, row) for r in reports for row in r.rows if row.status == status]
        if rows:
            print(f"\n{status}:")
            for suite, row in rows:
                print(f"  {suite}/{row.case}" + (f": {row.note}" if row.note else ""))
    print(f"\nTook {seconds:.0f}s. Results are in framework/var/results/.")


def main() -> int:
    """Run the suites that the arguments ask for, or list them."""
    args = sys.argv[1:]
    if args[:1] in (["-h"], ["--help"]):
        print((__doc__ or "").strip("\n"))
        return 0
    listing = args[:1] in (["-l"], ["--list"])
    if listing:
        args = args[1:]
    for arg in args:
        if arg.startswith("-"):
            common.die_usage(f"unknown option: {arg}")
    chosen = common.select(args, {name: list(m.SCENARIOS) for name, m in SUITES.items()})
    if listing:
        list_suites(chosen)
        return 0
    chosen = with_required(chosen)

    common.enter_var()
    if not args:
        # Only this run's: with RESULTS_TAG, another tag's are another run's
        # (as smoke.sh's rerun after rotating the keys is).
        for name in SUITES:
            with contextlib.suppress(FileNotFoundError):
                os.remove(Report(name, RESULTS).path)
    reports: list[Report] = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        session = Session(tmp)
        print(f"Testing {session.fed.url}: {describe(session.fed)}")
        for name, selected in chosen.items():
            report, finished = run_suite(name, selected, session)
            reports.append(report)
            required = getattr(SUITES[name], "REQUIRED", ())
            if not finished and required:
                print(
                    f"\n{common.PROG}: stopping: {name} did not finish, so nothing else can pass",
                    file=sys.stderr,
                )
                break
    summarize(reports, time.monotonic() - started)
    return 1 if any(r.failed for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
