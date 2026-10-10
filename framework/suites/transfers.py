"""Transfers through the clients: each scenario's batches of objects,
each batch one `pelican object get` or `put`, run together. The plugin-*
scenarios run the same batches through stash_plugin instead, and the
direct-* scenarios get from an origin rather than a cache. Then compare
every byte moved with the origins' stores, and check that no batch left
anything else in its directory, nor a failed get any file; and outside
the tiny topology, check that in each namespace some cache served the
downloads meant for one, and only an origin the direct ones.

What each scenario expects, how many batches it runs, and how a batch
is judged, is in framework/testlib/transfers.py. Results go to
framework/var/data/transfers, by <scenario>/<batch>: input/ (the batch,
in stash_plugin's format), output/ (the pelican client's transfer
stats, or the plugin's result ads), log/ (client logs, ending with the
exit status), files/ (what each batch downloaded or uploaded), and
verify/problems (every unexpected result).
"""

import collections
import json
import os
import random
import re
import shutil
import subprocess  # nosec B404
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from testlib import common, credentials, stores, transfers
from testlib.common import die, warn
from testlib.report import FAIL, PASS, SKIP, Report
from testlib.session import CLIENT_TIMEOUT, TIMED_OUT, Session, timed_out
from testlib.transfers import SCENARIOS as TABLE
from testlib.transfers import Batch, Observation, Outcome, Scenario

OUT = "data/transfers"


SCENARIOS = {s.name: s.description for s in TABLE}


def skip(_fed: common.Federation) -> Optional[str]:
    """Never: every shape exports a namespace that some scenario uses."""
    return None


def token_path(scenario: Scenario) -> Optional[str]:
    """Where the plugin finds the scenario's token, if it has one. The
    plugin chooses among every file in its credentials directory, so each
    scenario gets its own directory, with at most one token in it."""
    if scenario.credential is None or scenario.credential.key is None:
        return None
    return f".condor_creds/{scenario.name}/{scenario.credential.name}.use"


def hash_files(directory: str, prefix: str) -> dict[str, str]:
    """The SHA-256 of each file directly in directory, by prefix/<name>,
    e.g. protected-a/data/0.17."""
    hashes: dict[str, str] = {}
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return hashes
    for name in names:
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            hashes[f"{prefix}/{name}"] = common.sha256(path) or ""
    return hashes


class Plan:
    """The scenarios that run, their batches and tokens, and how the
    batches went."""

    def __init__(self, scenarios: list[Scenario], batches: dict[str, list[Batch]]):
        self.scenarios = scenarios
        self.batches = batches  # by scenario
        self.token_files: dict[str, Optional[str]] = {}  # by scenario
        self.statuses: dict[str, int] = {}  # exit status, by batch
        self.killed: set[str] = set()  # batches that timed out

    @property
    def puts(self) -> list[Scenario]:
        """The scenarios that upload."""
        return [s for s in self.scenarios if s.op == "put"]

    def all_batches(self) -> list[Batch]:
        """Every batch, scenario by scenario."""
        return [batch for s in self.scenarios for batch in self.batches[s.name]]


def runnable(fed: common.Federation, selected: list[str], report: Report) -> list[Scenario]:
    """The selected scenarios, each as it runs in this shape (see
    transfers.placed()), but for those that can test nothing here, which
    are recorded as skipped: those in a namespace the shape does not
    export, and those whose credential is moot."""
    scenarios: list[Scenario] = []
    moot: dict[str, str] = {}
    for scenario in (transfers.placed(transfers.BY_NAME[n], fed.exports) for n in selected):
        why: Optional[str] = None
        if not transfers.exported(scenario, fed.exports):
            why = f"this shape does not export /{scenario.namespace}"
        elif scenario.credential is not None:
            why = credentials.moot(fed, scenario.credential)
        if why:
            moot[scenario.name] = why
            report.add(scenario.name, SKIP, why, seconds=None)
        else:
            scenarios.append(scenario)
    for why, count in Counter(moot.values()).items():
        warn(f"skipping {count} scenarios: {why}")
    return scenarios


def make_directories(fed: common.Federation, plan: Plan) -> None:
    """Start framework/var/data/transfers afresh, with each batch in
    stash_plugin's input format, and a credentials directory for each
    scenario. Dies if a store lacks a put scenario's collection."""
    for origin in stores.unique(fed.origins):
        for s in plan.puts:
            directory = f"{origin.store_of(s.namespace)}/{s.store_dir}"
            if not origin.pstore and not os.path.isdir(directory):
                die(f"framework/var/{directory} is missing; run ./fed.sh init")
    for sub in ("input", "output", "log", "files", "verify"):
        shutil.rmtree(f"{OUT}/{sub}", ignore_errors=True)
    shutil.rmtree(".condor_creds", ignore_errors=True)
    for s in plan.scenarios:
        for sub in ("input", "output", "log", "files"):
            os.makedirs(f"{OUT}/{sub}/{s.name}")
        os.makedirs(f".condor_creds/{s.name}")
    for batch in plan.all_batches():
        os.makedirs(f"{OUT}/files/{batch.id}")
        with open(f"{OUT}/input/{batch.id}", "w", encoding="utf-8") as f:
            f.write(transfers.format_batch(batch))
    os.makedirs(f"{OUT}/verify")


def find_tokens(session: Session, plan: Plan) -> None:
    """Each scenario's token (see testlib/credentials.py), from the
    session, which minted the expired ones as it started; and a copy in
    the scenario's credentials directory, where the plugin finds it."""
    print(f"Creating tokens for {session.fed.url} ...")
    for s in plan.scenarios:
        path = None
        if s.credential is not None:
            path = session.credential_file(s.credential, s.namespace, s.op)
        plan.token_files[s.name] = path
        plugin_path = token_path(s)
        if path is not None and plugin_path is not None:
            shutil.copyfile(path, plugin_path)


def seed_pstore(
    origin: common.Origin, exports: transfers.Exports, tokens: dict[str, str]
) -> None:
    """Upload the test objects to a pstore origin, from the plain copy of
    each namespace with storage of its own there, since nothing else can
    write it. They go straight to the origin rather than through the
    director: under `topo-multi-origin`, each origin needs every object.
    Objects it already holds, byte for byte, are skipped."""

    def seed_one(job: tuple[str, str]) -> Optional[str]:
        namespace, rel = job
        with open(f"{origin.store_of(namespace)}/{rel}", "rb") as f:
            data = f.read()
        try:
            if stores.held(origin, namespace, rel, tokens[namespace]) == common.digest(data):
                return None
        except stores.Unreadable as e:
            return str(e)
        return stores.put(origin, namespace, rel, data, tokens[namespace])

    print(f"Seeding {origin.svc} from its plain copy in framework/var/{origin.store} ...")
    jobs: list[tuple[str, str]] = []
    for namespace in stores.storage_namespaces(origin, exports):
        directory = f"{origin.store_of(namespace)}/data"
        names = sorted(n for n in os.listdir(directory) if os.path.isfile(f"{directory}/{n}"))
        jobs += [(namespace, f"data/{name}") for name in names]
    with ThreadPoolExecutor(8) as pool:
        errors = [e for e in pool.map(seed_one, jobs) if e]
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        die(f"could not seed {origin.svc}; is it up (./fed.sh status)?")


def empty_put_collections(fed: common.Federation, plan: Plan, tokens: dict[str, str]) -> None:
    """Remove old uploads from every store (a pstore origin's through the
    namespace whose storage the scenario's reads), so that none can pass
    for a new one, or fail a put that should be refused."""
    print("Emptying the put collections ...")

    def empty(scenario: Scenario) -> list[str]:
        errors: list[str] = []
        for origin in stores.unique(fed.origins):
            namespace = origin.storage_namespace(scenario.namespace)
            errors += stores.empty_tree(
                origin, namespace, scenario.store_dir, tokens.get(namespace, "")
            )
        return errors

    with ThreadPoolExecutor(8) as pool:
        errors = [e for found in pool.map(empty, plan.puts) for e in found]
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        die("could not empty the put collections; are the origins up (./fed.sh status)?")


def write_sources(plan: Plan) -> None:
    """Fill each put batch's directory with new random files, of 1 byte to
    64 KiB."""
    size = 0
    for s in plan.puts:
        for batch in plan.batches[s.name]:
            for name in batch.names:
                size = (size + 7919) % 65536
                with open(f"{OUT}/files/{batch.id}/{name}", "wb") as source:
                    source.write(os.urandom(size + 1))


def command(session: Session, plan: Plan, batch: Batch) -> tuple[list[str], dict[str, str]]:
    """The client command that runs batch, and its environment."""
    s = batch.scenario
    output = os.path.abspath(f"{OUT}/output/{batch.id}")
    env = dict(session.env)
    if s.client == "plugin":
        # A direct get's URLs carry ?directread (see transfers.batch_url).
        env["_CONDOR_CREDS"] = os.path.abspath(f".condor_creds/{s.name}")
        args = [session.plugin]
        if s.op == "put":
            args.append("-upload")
        args += [
            "-infile",
            os.path.abspath(f"{OUT}/input/{batch.id}"),
            "-outfile",
            f"{output}.ad",
        ]
        return args, env
    args = [session.pelican, "object", s.op, "--transfer-stats", f"{output}.json"]
    token = plan.token_files[s.name]
    if token:
        args += ["--token", os.path.abspath(token)]
    if s.direct:
        args.append("--direct")
    if s.op == "get":
        args += [*batch.urls, "."]
    else:
        args += [*batch.names, batch.destination]
    return args, env


def transfer(session: Session, plan: Plan, batch: Batch) -> None:
    """Run batch in its directory, where its files are, with no terminal:
    with no token, `pelican object` would try to acquire one
    interactively. A batch that outlasts CLIENT_TIMEOUT is killed."""
    args, env = command(session, plan, batch)
    with open(f"{OUT}/log/{batch.id}", "wb") as log:
        try:
            status = subprocess.run(  # nosec B603
                args,
                cwd=f"{OUT}/files/{batch.id}",
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=CLIENT_TIMEOUT,
                check=False,
            ).returncode
        except subprocess.TimeoutExpired:
            plan.killed.add(batch.id)
            status = TIMED_OUT
            log.write(f"\n{timed_out(CLIENT_TIMEOUT)}\n".encode())
        log.write(f"\nexit status {status}\n".encode())
    plan.statuses[batch.id] = status


def transfer_all(session: Session, plan: Plan) -> float:
    """Run every batch, transfers.CONCURRENCY at a time: round <x> of
    every scenario, shuffled so that the scenarios and clients mix, then
    round <x>+1. Returns how many seconds they took."""
    rounds: dict[int, list[Batch]] = collections.defaultdict(list)
    for batch in plan.all_batches():
        rounds[batch.index].append(batch)
    order: list[Batch] = []
    for x in sorted(rounds):
        random.shuffle(rounds[x])
        order.extend(rounds[x])

    print(f"\nStarting {len(order)} transfers, {transfers.CONCURRENCY} at a time ...\n")
    started = time.monotonic()
    with ThreadPoolExecutor(transfers.CONCURRENCY) as pool:
        running = [pool.submit(transfer, session, plan, batch) for batch in order]
        for done, future in enumerate(as_completed(running), 1):
            future.result()
            if done % 100 == 0:
                print(f"  finished {done} of {len(order)}")
    return time.monotonic() - started


def observe(plan: Plan, batch: Batch) -> Observation:
    """What batch did: what its client said, and what its directory holds.
    One that was killed failed, however far it got, and not by refusal:
    its log says it timed out."""
    s = batch.scenario
    if s.client == "plugin":
        try:
            with open(f"{OUT}/output/{batch.id}.ad", encoding="utf-8") as f:
                said = f.read()
        except FileNotFoundError:
            said = ""
        passed = transfers.plugin_passed(said)
    else:
        with open(f"{OUT}/log/{batch.id}", encoding="utf-8", errors="replace") as f:
            said = f.read()
        passed = plan.statuses[batch.id] == 0
    failure = None
    if batch.id in plan.killed:
        passed, failure = False, "other"
    elif not passed:
        lines = transfers.failure_lines(s.client, said)
        failure = transfers.combine(transfers.classify(line) for line in lines)
    directory = f"{OUT}/files/{batch.id}"
    local: dict[str, str] = {}
    for name in batch.names:
        digest = common.sha256(f"{directory}/{name}")
        if digest is not None:
            local[name] = digest
    others = tuple(sorted(set(os.listdir(directory)) - set(batch.names)))
    return Observation(passed, failure, local, others)


def reference_hashes(fed: common.Federation) -> dict[str, str]:
    """The SHA-256 of each test object, by its path in the federation (see
    transfers.object_path()), from its namespace's storage at each origin
    (a pstore origin's plain copy stands in for its store)."""
    refs: dict[str, str] = {}
    for origin in stores.unique(fed.origins):
        for namespace in fed.exports:
            directory = f"{origin.store_of(namespace)}/data"
            for path, digest in hash_files(directory, f"{namespace}/data").items():
                refs.setdefault(path, digest)
    return refs


def uploaded_hashes(
    fed: common.Federation, plan: Plan, tokens: dict[str, str]
) -> tuple[dict[str, set[str]], list[str]]:
    """What the stores hold in the put collections: the SHA-256 of each
    object at any origin, by its path in the federation (see
    transfers.object_path()); and the pstore origins that could not say.
    A pstore origin's uploads are fetched from it directly, which also
    bypasses any cache."""
    held: dict[str, set[str]] = collections.defaultdict(set)
    for origin in stores.unique(fed.origins):
        if origin.pstore:
            continue
        for s in plan.puts:
            directory = f"{origin.store_of(s.namespace)}/{s.store_dir}"
            for path, digest in hash_files(directory, f"{s.namespace}/{s.store_dir}").items():
                held[path].add(digest)

    jobs = [
        (origin, s.namespace, transfers.rel_path(url))
        for origin in fed.origins
        if origin.pstore
        for s in plan.puts
        for batch in plan.batches[s.name]
        for url in batch.urls
    ]

    def read_back(job: tuple[common.Origin, str, str]) -> tuple[Optional[str], str]:
        origin, namespace, rel = job
        try:
            return stores.held(origin, namespace, rel, tokens[namespace]), ""
        except stores.Unreadable as e:
            return None, str(e)

    with ThreadPoolExecutor(8) as pool:
        found = list(pool.map(read_back, jobs))
    unreadable: list[str] = []
    for (origin, namespace, rel), (at_origin, trouble) in zip(jobs, found):
        if at_origin:
            held[f"{namespace}/{rel}"].add(at_origin)
        if trouble:
            print(trouble, file=sys.stderr)
            if origin.svc not in unreadable:
                unreadable.append(origin.svc)
    return held, unreadable


def judge_all(
    plan: Plan,
    expected: dict[str, Outcome],
    refs: dict[str, str],
    held: dict[str, set[str]],
    report: Report,
) -> bool:
    """Judge each batch (see testlib/transfers.py), and record a row per
    scenario. A scenario with unexpected results also names the sizes of
    the batches that had them. Returns whether every batch went as
    expected."""
    ok = True
    width = max(len(s.name) for s in plan.scenarios)
    print(
        f"\n{'scenario':<{width}} {'batches':>7} {'passed':>7} {'failed':>7}"
        + f" {'unexpected':>10}   {'expected':<10} failures"
    )
    last_group = None
    with open(f"{OUT}/verify/problems", "w", encoding="utf-8") as problems_file:
        for s in plan.scenarios:
            passed = failed = unexpected = 0
            failures: list[str] = []
            bad_sizes: set[int] = set()
            for batch in plan.batches[s.name]:
                observation = observe(plan, batch)
                if observation.passed:
                    passed += 1
                else:
                    failed += 1
                    failures.append(observation.failure or "other")
                problems = transfers.judge(batch, expected[s.name], observation, refs, held)
                for problem in problems:
                    problems_file.write(f"{batch.id} (size {len(batch.urls)}): {problem}\n")
                if problems:
                    unexpected += 1
                    bad_sizes.add(len(batch.urls))
            ok = ok and not unexpected
            note = transfers.summarize_failures(failures)
            if bad_sizes:
                sizes = ", ".join(str(n) for n in sorted(bad_sizes))
                note = f"{note}; " if note else ""
                note += f"unexpected in batches of {sizes}"
            group = (s.client, s.op, s.route)
            if last_group is not None and group != last_group:
                print()
            last_group = group
            count = len(plan.batches[s.name])
            print(
                (
                    f"{s.name:<{width}} {count:>7} {passed:>7} {failed:>7}"
                    + f" {unexpected:>10}   {expected[s.name].value:<10} {note}"
                ).rstrip()
            )
            summary = (
                f"expected {expected[s.name].value}; {count} batches,"
                f" {passed} passed, {failed} failed, {unexpected} unexpected"
            )
            status = FAIL if unexpected else PASS
            report.add(s.name, status, f"{summary}; {note}" if note else summary, seconds=None)
    if os.path.getsize(f"{OUT}/verify/problems"):
        print(f"\nProblems are listed in framework/var/{OUT}/verify/problems.")
    return ok


def served_by(batch: Batch) -> list[str]:
    """The host that served each object the batch moved: in the plugin's
    result ads, the highest-numbered EndpointN of each successful
    transfer; in the pelican client's transfer stats, which it writes only
    if every object moved, the endpoint of each result's highest-numbered
    attempt."""
    if batch.scenario.client == "plugin":
        try:
            with open(f"{OUT}/output/{batch.id}.ad", encoding="utf-8") as f:
                ads = f.read().splitlines()
        except FileNotFoundError:
            return []
        hosts: list[str] = []
        for ad in ads:
            if "TransferSuccess = true" not in ad:
                continue
            endpoints = re.findall(r'Endpoint(\d+) = "([^":]*)', ad)
            if endpoints:
                hosts.append(max(endpoints, key=lambda e: int(e[0]))[1])
        return hosts

    try:
        with open(f"{OUT}/output/{batch.id}.json", encoding="utf-8") as f:
            stats = json.load(f)
    except (FileNotFoundError, ValueError):
        return []
    return transfers.stats_endpoints(stats)


def check_served(
    fed: common.Federation, plan: Plan, expected: dict[str, Outcome], report: Report
) -> bool:
    """Which servers served the successful downloads, by client and
    route, judged by transfers.check_served_by(): outside `tiny`, a cache
    must have served some of those meant for one in each namespace, and
    an origin every direct one, and every one where there is no cache.
    Uploads go straight to an origin, so they don't count. Returns
    whether every verdict passed or skipped."""
    ok = True
    origin_hosts = {o.svc for o in fed.origins}
    header = False
    for client in ("pelican", "plugin"):
        for route in ("cache", "direct"):
            gets = [
                s
                for s in plan.scenarios
                if s.client == client and s.op == "get" and s.route == route
            ]
            downloads = [s for s in gets if expected[s.name] is Outcome.PASS]
            case = f"served-by-{'cache' if route == 'cache' else 'origin'}-{client}"
            if gets and not downloads:
                why = f"no {route} download should succeed in this shape"
                report.add(case, SKIP, why, seconds=None)
            if not downloads:
                continue
            served: dict[str, Counter[str]] = collections.defaultdict(Counter)
            for s in downloads:
                for batch in plan.batches[s.name]:
                    served[s.namespace].update(served_by(batch))
            hosts: Counter[str] = Counter()
            for counts in served.values():
                hosts.update(counts)
            if not header:
                print("\nServed by (successful downloads):")
                header = True
            label = f"{client} ({route})"
            if not hosts:
                print(f"  {label:<17}   (none recorded)")
            for host, count in sorted(hosts.items()):
                print(f"  {label:<17} {count:>6} {host}")

            verdict, note = transfers.check_served_by(
                route, served, origin_hosts, fed.tiny, not fed.caches
            )
            if verdict == FAIL:
                warn(f"{note} ({client})")
                ok = False
            report.add(case, verdict, note, seconds=None)
    return ok


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Prepare the selected scenarios' batches, run them all, and judge
    each one."""
    report = results.report
    fed = session.fed
    scenarios = runnable(fed, selected, report)
    if not scenarios:
        return
    expected = {s.name: transfers.outcome(s, fed.exports) for s in scenarios}
    # The batches are built afresh for each run (see transfers.LOAD).
    made = transfers.make_batches(fed.url, fed.exports, transfers.LOAD, random.randrange)
    plan = Plan(scenarios, {s.name: made[s.name] for s in scenarios})

    make_directories(fed, plan)
    find_tokens(session, plan)
    # The server's own tokens, for seeding and checking pstore origins.
    tokens = session.tokens()
    if any(s.op == "get" for s in scenarios):
        for origin in (o for o in fed.origins if o.pstore):
            seed_pstore(origin, fed.exports, tokens)
    if plan.puts:
        empty_put_collections(fed, plan, tokens)
    print(f"Preparing {len(plan.all_batches())} transfer batches ...")
    write_sources(plan)
    session.wait_until_stale()

    elapsed = transfer_all(session, plan)

    print("\nComparing bytes with the origins ...")
    held, unreadable = uploaded_hashes(fed, plan, tokens)
    ok = judge_all(plan, expected, reference_hashes(fed), held, report)
    if unreadable:
        warn(f"could not read every upload back from: {' '.join(unreadable)}")
        why = f"could not read uploads back from {' '.join(unreadable)}"
        report.add("read-back", FAIL, why, seconds=None)
        ok = False
    ok = check_served(fed, plan, expected, report) and ok

    print(f"\nThe transfers took {elapsed:.0f}s.")
    if ok:
        print(f"OK. Results are in framework/var/{OUT}/.")
    else:
        print(f"\nUNEXPECTED RESULTS. See framework/var/{OUT}/.")
