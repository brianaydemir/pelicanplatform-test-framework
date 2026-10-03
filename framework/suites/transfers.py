"""Transfers through the clients: each scenario's batches of objects,
each batch one `pelican object get` or `put`, run together. The plugin-*
scenarios run the same batches through stash_plugin instead, and the
direct-* scenarios get from an origin rather than a cache. Then compare
every byte moved with the origins' stores; and outside the tiny
topology, check that in each namespace some cache served the downloads
meant for one, and only an origin the direct ones.

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
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Set

from testlib import common, credentials, stores, transfers, web
from testlib.common import die, warn
from testlib.report import FAIL, PASS, SKIP
from testlib.session import CLIENT_TIMEOUT, TIMED_OUT, Session, timed_out
from testlib.transfers import SCENARIOS as TABLE
from testlib.transfers import Batch, Observation, Outcome, Scenario

OUT = "data/transfers"


SCENARIOS = {s.name: s.description for s in TABLE}


def token_path(scenario: Scenario) -> Optional[str]:
    """Where the plugin finds the scenario's token, if it has one. The
    plugin chooses among every file in its credentials directory, so each
    scenario gets its own directory, with at most one token in it."""
    if scenario.credential is None or scenario.credential.key is None:
        return None
    return f".condor_creds/{scenario.name}/{scenario.credential.name}.use"


def hash_files(directory: str, prefix: str) -> Dict[str, str]:
    """The SHA-256 of each file directly in directory, by prefix/<name>."""
    hashes: Dict[str, str] = {}
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return hashes
    for name in names:
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            hashes[f"{prefix}/{name}"] = common.sha256(path) or ""
    return hashes


def skip(fed: common.Federation) -> Optional[str]:
    return None


def run(session: Session, selected: List[str], results: common.Results) -> None:
    report = results.report
    pelican, plugin = session.pelican, session.plugin
    fed = session.fed
    origins = fed.origins
    exports = fed.exports
    # Each as it runs in this shape: `dne` moves where /public isn't
    # exported (see transfers.placed()).
    scenarios = [transfers.placed(transfers.BY_NAME[n], exports) for n in selected]

    # Skip the scenarios that can test nothing in this shape: those in a
    # namespace it does not export, and those whose credential is moot.
    moot: Dict[str, str] = {}
    for s in scenarios:
        if not transfers.exported(s, exports):
            why: Optional[str] = f"this shape does not export /{s.namespace}"
        else:
            why = credentials.moot(fed, s.credential) if s.credential else None
        if why:
            moot[s.name] = why
            report.add(s.name, SKIP, why, seconds=None)
    if moot:
        for why in dict.fromkeys(moot.values()):
            count = sum(1 for w in moot.values() if w == why)
            warn(f"skipping {count} scenarios: {why}")
        scenarios = [s for s in scenarios if s.name not in moot]
        if not scenarios:
            return
    any_get = any(s.op == "get" for s in scenarios)
    any_put = any(s.op == "put" for s in scenarios)
    expected = {s.name: transfers.outcome(s, exports) for s in scenarios}
    env = session.env

    # The batches, built afresh for each run (see transfers.LOAD), and
    # written out in stash_plugin's input format.
    made = transfers.make_batches(fed.url, exports, transfers.LOAD, random.randrange)
    batches: Dict[str, List[Batch]] = {s.name: made[s.name] for s in scenarios}
    total = sum(len(b) for b in batches.values())

    # Where each test object's bytes are (a pstore origin's plain copy
    # stands in for its store), the stores readable on disk, and the pstore
    # origins, whose stores are encrypted.
    ref_dirs = list(dict.fromkeys(o.store for o in origins))
    disk_stores = list(dict.fromkeys(o.store for o in origins if not o.pstore))
    pstores = [o for o in origins if o.pstore]

    put_scenarios = [s for s in scenarios if s.op == "put"]
    for store in disk_stores:
        for s in put_scenarios:
            if not os.path.isdir(f"{store}/{s.store_dir}"):
                die(f"framework/var/{store}/{s.store_dir} is missing; run ./fed.sh init")

    for sub in ("input", "output", "log", "files", "verify"):
        shutil.rmtree(f"{OUT}/{sub}", ignore_errors=True)
    shutil.rmtree(".condor_creds", ignore_errors=True)
    for s in scenarios:
        for sub in ("input", "output", "log", "files"):
            os.makedirs(f"{OUT}/{sub}/{s.name}")
        os.makedirs(f".condor_creds/{s.name}")
        for batch in batches[s.name]:
            with open(f"{OUT}/input/{batch.id}", "w") as f:
                f.write(transfers.format_batch(batch))
    os.makedirs(f"{OUT}/verify")

    #-----------------------------------------------------------------------
    # Tokens (see testlib/credentials.py), from the session, which minted
    # the expired ones as it started. The plugin finds each in its
    # scenario's credentials directory.

    print(f"Creating tokens for {fed.url} ...")
    token_files: Dict[str, Optional[str]] = {}
    for s in scenarios:
        path = None
        if s.credential is not None:
            path = session.credential_file(s.credential, s.namespace, s.op)
        token_files[s.name] = path
        plugin_path = token_path(s)
        if path is not None and plugin_path is not None:
            shutil.copyfile(path, plugin_path)
    # For the test itself: seeding and checking pstore origins.
    tokens = session.tokens()

    #-----------------------------------------------------------------------
    # Seed each pstore origin with the test objects. Nothing else can write
    # its store, so upload the plain copy beside it, straight to the origin
    # rather than through the director: under `topo-multi-origin`, each
    # origin needs every object. Objects it already has are skipped; their
    # bytes depend only on their names.

    def seed_one(origin: common.Origin, namespace: str, name: str) -> Optional[str]:
        target = f"{origin.url}/{namespace}/data/{name}"
        answer = web.request("HEAD", target, token=tokens[namespace])
        if answer.status == 200:
            return None
        if answer.status != 404:
            return f"HEAD {target}: {answer.describe()}"
        with open(f"{origin.store}/data/{name}", "rb") as f:
            return stores.put(origin, namespace, f"data/{name}", f.read(), tokens[namespace])

    if pstores and any_get:
        for origin in pstores:
            print(f"Seeding {origin.svc} from framework/var/{origin.store}/data ...")
            objects = sorted(n for n in os.listdir(f"{origin.store}/data")
                             if os.path.isfile(f"{origin.store}/data/{n}"))
            with ThreadPoolExecutor(8) as pool:
                errors = [e for e in pool.map(lambda job: seed_one(origin, *job),
                                              [(ns, n) for n in objects
                                               for ns in stores.upload_namespaces(
                                                   origin, exports)]) if e]
            for error in errors:
                print(error, file=sys.stderr)
            if errors:
                die(f"could not seed {origin.svc}; is it up (./fed.sh status)?")

    #-----------------------------------------------------------------------
    # Prepare each batch's directory: empty for a get, new random bytes (1
    # byte to 64 KiB) for a put. Old uploads come out of every store first,
    # a pstore origin's in each namespace with storage of its own there, so
    # none can pass for a new one, or fail a put that should be refused.

    if put_scenarios:
        print("Emptying the put collections ...")
        with ThreadPoolExecutor(8) as pool:
            errors = [e for found in pool.map(
                lambda s: stores.empty_tree_everywhere(origins, exports, s.store_dir, tokens),
                put_scenarios) for e in found]
        for error in errors:
            print(error, file=sys.stderr)
        if errors:
            die("could not empty the put collections; are the origins up (./fed.sh status)?")

    print(f"Preparing {total} transfer batches ...")
    size = 0
    for s in scenarios:
        for batch in batches[s.name]:
            os.makedirs(f"{OUT}/files/{batch.id}")
            if s.op != "put":
                continue
            for name in batch.names:
                size = (size + 7919) % 65536
                with open(f"{OUT}/files/{batch.id}/{name}", "wb") as source:
                    source.write(os.urandom(size + 1))

    session.wait_until_stale()

    #-----------------------------------------------------------------------
    # Run the transfers: round <x> of every scenario, shuffled so that the
    # scenarios and clients mix, then round <x>+1. Each runs in its batch's
    # directory, where its files are. Neither client may see a terminal:
    # with no token, `pelican object` would try to acquire one
    # interactively. A batch that outlasts CLIENT_TIMEOUT is killed, and
    # fails (see observe()).

    rounds: Dict[int, List[Batch]] = collections.defaultdict(list)
    for s in scenarios:
        for batch in batches[s.name]:
            rounds[batch.index].append(batch)
    order: List[Batch] = []
    for x in sorted(rounds):
        random.shuffle(rounds[x])
        order.extend(rounds[x])

    killed: Set[str] = set()

    def transfer(batch: Batch) -> int:
        s = batch.scenario
        output = os.path.abspath(f"{OUT}/output/{batch.id}")
        batch_env = dict(env)
        token = token_files[s.name]
        if s.client == "plugin":
            # A direct get's URLs carry ?directread (see
            # transfers.batch_url).
            batch_env["_CONDOR_CREDS"] = os.path.abspath(f".condor_creds/{s.name}")
            command = [plugin, *(["-upload"] if s.op == "put" else []),
                       "-infile", os.path.abspath(f"{OUT}/input/{batch.id}"),
                       "-outfile", f"{output}.ad"]
        else:
            command = [pelican, "object", s.op,
                       *(["--token", os.path.abspath(token)] if token else []),
                       *(["--direct"] if s.direct else []),
                       "--transfer-stats", f"{output}.json"]
            if s.op == "get":
                command += [*batch.urls, "."]
            else:
                command += [*batch.names, batch.destination]
        with open(f"{OUT}/log/{batch.id}", "wb") as log:
            try:
                status = subprocess.run(command, cwd=f"{OUT}/files/{batch.id}", env=batch_env,
                                        stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT,
                                        timeout=CLIENT_TIMEOUT).returncode
            except subprocess.TimeoutExpired:
                killed.add(batch.id)
                status = TIMED_OUT
                log.write(f"\n{timed_out(CLIENT_TIMEOUT)}\n".encode())
            log.write(f"\nexit status {status}\n".encode())
        return status

    print(f"\nStarting {total} transfers, {transfers.CONCURRENCY} at a time ...\n")
    started = time.monotonic()
    statuses: Dict[str, int] = {}
    with ThreadPoolExecutor(transfers.CONCURRENCY) as pool:
        running = {pool.submit(transfer, batch): batch for batch in order}
        for done, future in enumerate(as_completed(running), 1):
            statuses[running[future].id] = future.result()
            if done % 100 == 0:
                print(f"  finished {done} of {total}")
    elapsed = time.monotonic() - started

    #-----------------------------------------------------------------------
    # Observe: what each client said, and what each batch's directory
    # holds; where the test objects' bytes are; and what each store's put
    # collections hold. A pstore origin's uploads are fetched from it
    # directly, which also bypasses any cache.

    print("\nComparing bytes with the origins ...")

    def observe(batch: Batch) -> Observation:
        """What batch did. One that was killed failed, however far it
        got, and not by refusal: its log says it timed out."""
        s = batch.scenario
        if s.client == "plugin":
            try:
                with open(f"{OUT}/output/{batch.id}.ad") as f:
                    said = f.read()
            except FileNotFoundError:
                said = ""
            passed = transfers.plugin_passed(said)
        else:
            with open(f"{OUT}/log/{batch.id}", errors="replace") as f:
                said = f.read()
            passed = statuses[batch.id] == 0
        failure = None
        if batch.id in killed:
            passed, failure = False, "other"
        elif not passed:
            failure = transfers.combine(
                transfers.classify(line) for line in transfers.failure_lines(s.client, said))
        local = {}
        for name in batch.names:
            digest = common.sha256(f"{OUT}/files/{batch.id}/{name}")
            if digest is not None:
                local[name] = digest
        return Observation(passed, failure, local)

    refs: Dict[str, str] = {}
    for directory in ref_dirs:
        for rel, digest in hash_files(f"{directory}/data", "data").items():
            refs.setdefault(rel, digest)

    held: Dict[str, Set[str]] = collections.defaultdict(set)
    for store in disk_stores:
        for s in put_scenarios:
            for rel, digest in hash_files(f"{store}/{s.store_dir}", s.store_dir).items():
                held[rel].add(digest)

    unreadable: List[str] = []
    if pstores and any_put:
        wanted = [(s.namespace, transfers.rel_path(url)) for s in put_scenarios
                  for batch in batches[s.name] for url in batch.urls]
        for origin in pstores:

            def fetch(job):
                namespace, rel = job
                try:
                    return rel, stores.held(origin, namespace, rel, tokens[namespace]), None
                except stores.Unreadable as e:
                    return rel, None, str(e)

            with ThreadPoolExecutor(8) as pool:
                for rel, digest, trouble in pool.map(fetch, wanted):
                    if digest:
                        held[rel].add(digest)
                    if trouble:
                        print(trouble, file=sys.stderr)
                        if origin.svc not in unreadable:
                            unreadable.append(origin.svc)

    #-----------------------------------------------------------------------
    # Judge each batch (see testlib/transfers.py), and summarize. A
    # scenario with unexpected results also names the sizes of the batches
    # that had them.

    status = 0
    width = max(len(s.name) for s in scenarios)
    print(f"\n{'scenario':<{width}} {'batches':>7} {'passed':>7} {'failed':>7}"
          f" {'unexpected':>10}   {'expected':<10} failures")
    last_group = None
    with open(f"{OUT}/verify/problems", "w") as problems_file:
        for s in scenarios:
            passed = failed = unexpected = 0
            failures: List[str] = []
            bad_sizes: Set[int] = set()
            for batch in batches[s.name]:
                observation = observe(batch)
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
            if unexpected:
                status = 1
            note = transfers.summarize_failures(failures)
            if bad_sizes:
                sizes = ", ".join(str(n) for n in sorted(bad_sizes))
                note = f"{note}; " if note else ""
                note += f"unexpected in batches of {sizes}"
            group = (s.client, s.op, s.route)
            if last_group is not None and group != last_group:
                print()
            last_group = group
            print(f"{s.name:<{width}} {len(batches[s.name]):>7} {passed:>7} {failed:>7}"
                  f" {unexpected:>10}   {expected[s.name].value:<10} {note}".rstrip())
            summary = (f"expected {expected[s.name].value}; {len(batches[s.name])} batches,"
                       f" {passed} passed, {failed} failed, {unexpected} unexpected")
            report.add(s.name, FAIL if unexpected else PASS,
                       f"{summary}; {note}" if note else summary, seconds=None)

    if unreadable:
        warn(f"could not read every upload back from: {' '.join(unreadable)}")
        report.add("read-back", FAIL, f"could not read uploads back from {' '.join(unreadable)}",
                   seconds=None)
        status = 1
    if os.path.getsize(f"{OUT}/verify/problems"):
        print(f"\nProblems are listed in framework/var/{OUT}/verify/problems.")

    #-----------------------------------------------------------------------
    # Which servers served the successful downloads, by client and route,
    # judged by transfers.check_served_by(): outside `tiny`, a cache must
    # have served some of those meant for one in each namespace, and an
    # origin every direct one. The highest-numbered attempt at each object
    # served it. Uploads go straight to an origin, so they don't count.

    origin_hosts = {o.svc for o in origins}
    header = False
    for client in ("pelican", "plugin"):
        for route in ("cache", "direct"):
            gets = [s for s in scenarios if s.client == client and s.op == "get"
                    and s.route == route]
            downloads = [s for s in gets if expected[s.name] is Outcome.PASS]
            case = f"served-by-{'cache' if route == 'cache' else 'origin'}-{client}"
            if gets and not downloads:
                report.add(case, SKIP, f"no {route} download should succeed in this shape",
                           seconds=None)
            if not downloads:
                continue
            served: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
            for s in downloads:
                for batch in batches[s.name]:
                    served[s.namespace].update(served_by(batch))
            hosts: collections.Counter = sum(served.values(), collections.Counter())
            if not header:
                print("\nServed by (successful downloads):")
                header = True
            label = f"{client} ({route})"
            if not hosts:
                print(f"  {label:<17}   (none recorded)")
            for host, count in sorted(hosts.items()):
                print(f"  {label:<17} {count:>6} {host}")

            verdict, note = transfers.check_served_by(route, served, origin_hosts, fed.tiny)
            if verdict == FAIL:
                warn(f"{note} ({client})")
                status = 1
            report.add(case, verdict, note, seconds=None)

    print(f"\nThe transfers took {elapsed:.0f}s.")
    if status == 0:
        print(f"OK. Results are in framework/var/{OUT}/.")
    else:
        print(f"\nUNEXPECTED RESULTS. See framework/var/{OUT}/.")


def served_by(batch: Batch) -> List[str]:
    """The host that served each object the batch moved: in the plugin's
    result ads, the highest-numbered EndpointN of each successful
    transfer; in the pelican client's transfer stats, which it writes only
    if every object moved, the endpoint of each result's highest-numbered
    attempt."""
    hosts = []
    if batch.scenario.client == "plugin":
        try:
            with open(f"{OUT}/output/{batch.id}.ad") as f:
                ads = f.read().splitlines()
        except FileNotFoundError:
            return []
        for ad in ads:
            if "TransferSuccess = true" not in ad:
                continue
            endpoints = re.findall(r'Endpoint(\d+) = "([^":]*)', ad)
            if endpoints:
                hosts.append(max(endpoints, key=lambda e: int(e[0]))[1])
        return hosts

    try:
        with open(f"{OUT}/output/{batch.id}.json") as f:
            stats = json.load(f)
    except (FileNotFoundError, ValueError):
        return []
    return transfers.stats_endpoints(stats)
