#!/usr/bin/env python3.13
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

The suites are in framework/suites/, and run in the order that its
__init__.py gives (`-l` lists them). `federation/ready` and
`federation/caches` run first whatever is asked for; if the federation
never serves, nothing else runs. A suite that doesn't apply to the
shape, such as pstore without a pstore origin, is skipped.

Each pass of a suite writes a row per case to
framework/var/results/<suite>.tsv (or <suite>-scitokens.tsv). A run of
everything first removes the results of earlier runs with the same
RESULTS_TAG.

A pass that prints nothing for STALL_SECONDS, or runs for
BUDGET_SECONDS, is abandoned, so that a scenario stuck on a server that
never answers cannot stall the run. Where each of its threads was is
printed, and the pass is recorded as `aborted` (or `setup`, if it
recorded no case). If its cleanup takes over GRACE_SECONDS, that is
interrupted too.
"""

import contextlib
import faulthandler
import os
import signal
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Generator, Sequence
from typing import Any, TextIO

# No __pycache__ in the checkout, where the dev container would leave it
# root's.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "framework"))

# pylint: disable=wrong-import-position
from suites import SUITES
from testlib import common
from testlib.credentials import PROFILES, WLCG
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP, Report, redact
from testlib.session import Session

# pylint: enable=wrong-import-position

RESULTS = os.path.join(common.VAR, "results")

# A suite's pass is abandoned once it has printed nothing for this long
# (every suite prints each row as it records it, and the long-running
# ones their progress; the longest silence a healthy pass has is a few
# minutes), or once it has run for this long, however much it prints.
STALL_SECONDS = 20 * 60
BUDGET_SECONDS = 90 * 60
# Once abandoned, how long its cleanup (the suite's `finally` blocks,
# which put back what it restarted or wrote) may take before it is
# interrupted again, and again.
GRACE_SECONDS = 5 * 60


class Stalled(BaseException):
    """A suite's pass that the watchdog gave up on.

    Not an Exception: a suite catches those around each scenario, so
    that the next still runs, and must not catch this.
    """


class Activity:
    """A text stream that notes when it was last written to.

    It stands in for sys.stdout and sys.stderr, so that the watchdog can
    tell a pass that is getting on from one that is stuck. It also cuts
    the signature out of any token written (report.redact()), since
    what the tests print is kept with the logs.
    """

    last_write = time.monotonic()

    def __init__(self, stream: TextIO):
        self._stream = stream

    def write(self, text: str) -> int:
        """Write text, and note the time."""
        Activity.last_write = time.monotonic()
        return self._stream.write(redact(text))

    def writelines(self, lines: Any) -> None:
        """Write each of lines, and note the time."""
        Activity.last_write = time.monotonic()
        self._stream.writelines(redact(line) for line in lines)

    def __getattr__(self, name: str) -> Any:
        """Return the stream's attribute name."""
        return getattr(self._stream, name)


@contextlib.contextmanager
def watched(name: str) -> Generator[None, None, None]:
    """Abandon the pass of suite name within the block if it stalls.

    A thread watches the output and the clock; when the pass has printed
    nothing for STALL_SECONDS, or has run for BUDGET_SECONDS, it prints
    where every thread is, for the log, and sends the process SIGALRM,
    whose handler raises Stalled in the main thread, wherever it is: a
    blocked read or join included, since a signal interrupts those. The
    suite's cleanup runs as for any other error; if the pass is still
    going GRACE_SECONDS later, as when the cleanup waits on the same
    server, it is interrupted again, until it ends. SIGALRM is ignored
    outside the block, so a late one does no harm.
    """
    why: list[str] = []
    stop = threading.Event()
    started = time.monotonic()
    Activity.last_write = started

    def watch() -> None:
        while not stop.wait(5):
            now = time.monotonic()
            if now - started > BUDGET_SECONDS:
                why.append(f"{name} ran for {BUDGET_SECONDS}s, and was abandoned")
            elif now - Activity.last_write > STALL_SECONDS:
                why.append(f"{name} printed nothing for {STALL_SECONDS}s, and was abandoned")
            else:
                continue
            print(f"\n{common.PROG}: {why[0]}; where each thread was:", file=sys.stderr)
            print_stacks()
            os.kill(os.getpid(), signal.SIGALRM)
            while not stop.wait(GRACE_SECONDS):
                print(
                    f"\n{common.PROG}: {name} has not ended {GRACE_SECONDS}s after being"
                    + " abandoned; interrupting its cleanup",
                    file=sys.stderr,
                )
                os.kill(os.getpid(), signal.SIGALRM)
            return

    def alarm(_signum: int, _frame: Any) -> None:
        raise Stalled(why[0] if why else f"{name} was abandoned")

    signal.signal(signal.SIGALRM, alarm)
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        yield
    finally:
        # Ignored first, so that an alarm sent as the pass ended cannot
        # abandon it after the fact.
        signal.signal(signal.SIGALRM, signal.SIG_IGN)
        stop.set()
        watcher.join()


def print_stacks() -> None:
    """Print every thread's stack, most recent call first, to stderr."""
    sys.stderr.flush()
    stream = sys.__stderr__
    if stream is not None:
        faulthandler.dump_traceback(stream, all_threads=True)
        stream.flush()


def lingering() -> list[threading.Thread]:
    """Return the threads that would keep the interpreter from exiting.

    An abandoned pass may leave some running: a task its pool had begun,
    or one of its own, until a timeout of the task's ends it.
    """
    return [
        t
        for t in threading.enumerate()
        if t is not threading.main_thread() and not t.daemon and t.is_alive()
    ]


def current_shape() -> common.Federation | None:
    """Return the federation's shape, if fed.sh has written it, or None."""
    if not os.path.isfile(os.path.join(common.VAR, "generated", "origins")):
        return None
    common.enter_var()
    return common.Federation()


def required(name: str) -> Sequence[str]:
    """Return suite name's REQUIRED scenarios."""
    return getattr(SUITES[name], "REQUIRED", ())


def one_profile(name: str) -> Sequence[str]:
    """Return suite name's ONE_PROFILE scenarios."""
    return getattr(SUITES[name], "ONE_PROFILE", ())


def skip_reason(name: str, fed: common.Federation) -> str | None:
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
    return [s for s in selected if s not in one_profile(name)]


def list_suites(chosen: dict[str, list[str]]) -> None:
    """Print the chosen suites and scenarios, and which skip."""
    fed = current_shape()
    for name, scenarios in chosen.items():
        module = SUITES[name]
        why = skip_reason(name, fed) if fed else None
        print(name + (f" (skipped in this shape: {why})" if why else ""))
        width = max(len(n) for n in module.SCENARIOS)
        labels = (("always runs", required(name)), ("WLCG only", one_profile(name)))
        for scenario in scenarios:
            notes = [label for label, of in labels if scenario in of]
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
        always = required(name)
        if name in chosen or always:
            wanted = set(chosen.get(name, ())) | set(always)
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
    error: str | None = None
    try:
        why = module.skip(session.fed)
        if why:
            print(f"Skipping every scenario: {why}")
            for scenario in selected:
                report.add(scenario, SKIP, why, seconds=None)
        else:
            with watched(report.suite):
                module.run(session, selected, results)
    except common.Died as e:
        error = e.message
    except Stalled as e:
        error = str(e)
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


def run_passes(chosen: dict[str, list[str]], sessions: list[Session]) -> list[Report]:
    """Run each chosen suite's pass in each of sessions, in turn.

    Stops once a suite with REQUIRED scenarios does not finish in the
    first session's pass.

    Returns:
        Each pass's report, in the order the passes ran.
    """
    reports: list[Report] = []
    for name, selected in chosen.items():
        for session in sessions:
            scenarios = in_pass(name, selected, session.profile)
            if not scenarios:
                continue
            report, finished = run_suite(name, scenarios, session)
            reports.append(report)
            # The first pass shows whether the federation serves at all.
            if not finished and required(name) and session is sessions[0]:
                print(
                    f"\n{common.PROG}: stopping: {name} did not finish, so nothing else can pass",
                    file=sys.stderr,
                )
                return reports
    return reports


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
    sys.stdout = Activity(sys.stdout)
    sys.stderr = Activity(sys.stderr)
    signal.signal(signal.SIGALRM, signal.SIG_IGN)
    if not args:
        # Only this run's: with RESULTS_TAG, another tag's are another
        # run's.
        for name in SUITES:
            for profile in PROFILES:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(Report(suite_name(name, profile), RESULTS).path)
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        first = Session(tmp, profiles[0], run=stamp)
        sessions = [first] + [Session(tmp, p, first.fed, stamp) for p in profiles[1:]]
        print(f"Testing {first.fed.url}: {describe(first.fed)}")
        reports = run_passes(chosen, sessions)
    summarize(reports, time.monotonic() - started)
    status = 1 if any(r.failed for r in reports) else 0
    left = lingering()
    if left:
        # The interpreter would wait for each to end before exiting.
        common.warn(
            f"exiting without waiting for {len(left)} thread(s) that abandoned passes"
            + f" left running: {', '.join(t.name for t in left)}"
        )
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(status)
    return status


if __name__ == "__main__":
    sys.exit(main())
