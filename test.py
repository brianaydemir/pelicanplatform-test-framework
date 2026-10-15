#!/usr/bin/env python3
"""Run the basic functionality tests against the running federation.

Runs every suite that applies to the federation's shape, over every
combination of client, route, namespace, credential, and server that the
shape has. Exits non-zero if any case fails.

  ./test.py                          everything that applies
  ./test.py auth commands            only these suites
  ./test.py 'transfers/plugin-*'     only the scenarios of a suite that match
  ./test.py --profile wlcg [ARG]...  only the WLCG pass (or `scitokens`)
  ./test.py -l [SUITE]...            list the suites and their scenarios

Run it in the dev container, e.g. from the host:

  ./fed.sh test [ARG]...

Each suite runs twice, once for each token profile: its scenarios
present WLCG tokens, and then, as <suite>-scitokens, those that present
a storage token of the tests' own minting present SciTokens instead. A
suite's ONE_PROFILE scenarios, which present no such token, run in the
WLCG pass alone.

The suites are in framework/suites/, and run in this order: federation,
selftest, commands, client, transfer-api, listings, names, blocks,
freshness, cache-api, throttling, exports, pstore, routing,
director-api, registry, tiering, posc, metadata, users, owners,
sitelocal, collections, issuer, accounts, oidc, advertise, webapi,
observability, webdav, auth, transfers, restart, and keys.
`federation/ready` and `federation/caches` run first whatever is asked
for, and if the federation never serves, nothing else runs. A suite
that doesn't apply to the shape, such as posc without POSC or pstore, is
skipped. Each pass of a suite writes a row per case to
framework/var/results/<suite>.tsv (or <suite>-scitokens.tsv); a run of
everything first removes the results of earlier runs with the same
RESULTS_TAG.
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
from testlib.credentials import PROFILES, WLCG
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP, Report
from testlib.session import Session

# pylint: enable=wrong-import-position

RESULTS = os.path.join(common.VAR, "results")


def current_shape() -> Optional[common.Federation]:
    """Return the federation's shape, if fed.sh has written it, or None."""
    if not os.path.isfile(os.path.join(common.VAR, "generated", "origins")):
        return None
    common.enter_var()
    return common.Federation()


def skip_reason(name: str, fed: common.Federation) -> Optional[str]:
    """Return why suite name skips in fed's shape, or None if it doesn't.

    None too if the suite's skip() dies.
    """
    try:
        return SUITES[name].skip(fed)
    except common.Died:
        return None


def suite_name(name: str, profile: str) -> str:
    """Return how suite name's pass in profile is reported.

    That is name for WLCG's pass, and <name>-<profile> for another's.
    """
    return name if profile == WLCG else f"{name}-{profile}"


def in_pass(name: str, selected: list[str], profile: str) -> list[str]:
    """Return the selected scenarios of suite name that run in profile's pass.

    WLCG's pass runs every one; another's, all but the suite's
    ONE_PROFILE ones.
    """
    if profile == WLCG:
        return selected
    once = getattr(SUITES[name], "ONE_PROFILE", ())
    return [s for s in selected if s not in once]


def list_suites(chosen: dict[str, list[str]]) -> None:
    """Print the chosen suites and scenarios, and which skip."""
    fed = current_shape()
    for name, scenarios in chosen.items():
        module = SUITES[name]
        why = skip_reason(name, fed) if fed else None
        print(name + (f" (skipped in this shape: {why})" if why else ""))
        width = max(len(n) for n in module.SCENARIOS)
        required = getattr(module, "REQUIRED", ())
        once = getattr(module, "ONE_PROFILE", ())
        for scenario in scenarios:
            notes = [
                n
                for n, of in (("always runs", required), ("WLCG only", once))
                if scenario in of
            ]
            noted = f" ({'; '.join(notes)})" if notes else ""
            print(f"  {scenario:<{width}} {module.SCENARIOS[scenario]}{noted}")
        print()
    if fed is None:
        print("Run ./fed.sh init to see which suites apply to the shape.")


def with_required(chosen: dict[str, list[str]]) -> dict[str, list[str]]:
    """Return chosen, with each suite's REQUIRED scenarios added.

    Returns:
        Each suite that chosen names or that has REQUIRED scenarios, in
        the suites' order, with its chosen and REQUIRED scenarios, in
        their order.
    """
    found: dict[str, list[str]] = {}
    for name, module in SUITES.items():
        required = getattr(module, "REQUIRED", ())
        if name in chosen or required:
            wanted = set(chosen.get(name, ())) | set(required)
            found[name] = [n for n in module.SCENARIOS if n in wanted]
    return found


def describe(fed: common.Federation) -> str:
    """Return fed's shape, briefly."""
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
    """Run one suite's selected scenarios in session's pass; write its results.

    Returns:
        The pass's report, and whether the suite finished; if it didn't,
        its report says why.
    """
    module = SUITES[name]
    report = Report(suite_name(name, session.profile), RESULTS)
    results = common.Results(list(module.SCENARIOS), report)
    print(f"\n==> {report.suite}\n")
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
    """Print each suite's counts, then every failed or unsure case.

    Args:
        reports: Each suite's report, in the order the suites ran.
        seconds: How long the run took.
    """
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


def profile_option(args: list[str]) -> tuple[list[str], list[str]]:
    """Return the profiles that `--profile` chooses, and the other args.

    `--profile PROFILE`, or `--profile=PROFILE`, chooses one of PROFILES;
    without it, every one, WLCG's first.
    """
    chosen: list[str] = []
    rest: list[str] = []
    pending = list(args)
    while pending:
        arg = pending.pop(0)
        if arg == "--profile" and pending:
            arg = f"--profile={pending.pop(0)}"
        if not arg.startswith("--profile"):
            rest.append(arg)
            continue
        profile = arg.partition("=")[2]
        if profile not in PROFILES or chosen:
            common.die_usage(f"--profile takes one of {', '.join(PROFILES)}, once")
        chosen.append(profile)
    return chosen or list(PROFILES), rest


def main() -> int:
    """Run the suites that the arguments ask for, or list them.

    Returns:
        The exit status: 1 if any case failed, and 0 otherwise.
    """
    args = sys.argv[1:]
    if args[:1] in (["-h"], ["--help"]):
        print((__doc__ or "").strip("\n"))
        return 0
    listing = args[:1] in (["-l"], ["--list"])
    if listing:
        args = args[1:]
    profiles, args = profile_option(args)
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
        # Only this run's: with RESULTS_TAG, another tag's are another
        # run's.
        for name in SUITES:
            for profile in PROFILES:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(Report(suite_name(name, profile), RESULTS).path)
    reports: list[Report] = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        first = Session(tmp, profiles[0], run=stamp)
        sessions = [first] + [Session(tmp, p, first.fed, stamp) for p in profiles[1:]]
        print(f"Testing {first.fed.url}: {describe(first.fed)}")
        stopped = False
        for name, selected in chosen.items():
            for session in sessions:
                scenarios = in_pass(name, selected, session.profile)
                if not scenarios:
                    continue
                report, finished = run_suite(name, scenarios, session)
                reports.append(report)
                required = getattr(SUITES[name], "REQUIRED", ())
                # The first pass shows whether the federation serves at all.
                if not finished and required and session is first:
                    print(
                        f"\n{common.PROG}: stopping: {name} did not finish, so nothing else"
                        + " can pass",
                        file=sys.stderr,
                    )
                    stopped = True
                    break
            if stopped:
                break
    summarize(reports, time.monotonic() - started)
    return 1 if any(r.failed for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
