"""What every server reports about itself, and who may see it.

See docs/tests/observability.md.

The suite acts as each server's admin, and as an ordinary user
(testlib.webapi.Access). It reads /metrics with tokens of each server's
own making (credentials.web_token()), since every server guards it
(Monitoring.MetricAuthorization, on by default). `log-level` and
`log-redaction` raise each server's log level, each change for at most
a minute but the one `log-level` deletes at once, and delete their
changes when they are done. `log-level-xrootd` restarts XRootD twice at
the first XRootD origin and cache, and waits until it serves again.
`http-metrics` writes under data/observability/<run>/, which the suite
removes afterward.

Only `http-metrics` and `log-redaction` present storage tokens, of the
session's profile; the rest present tokens of the servers' web APIs,
which Pelican mints in the WLCG profile alone, and `log-level-xrootd`
sees XRootD serve again with a WLCG storage token, so they run in the
WLCG pass alone.
"""

import functools
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from testlib import accounts, common, credentials, director, jwt, metrics, stores, suite, web, webapi
from testlib.common import first, outcome, warn
from testlib.credentials import RWM, R
from testlib.director import expected_targets
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session, last_line
from testlib.suite import CaseRow
from testlib.webapi import API, CONFIG, HEALTH, LOG_LEVEL, LOG_TAIL, Server, each_server, judge

SCENARIOS = {
    "health-api": "each server's health report: for its admin alone, and every component ok",
    "health-metrics": "pelican_component_health_status agrees with each server's health report",
    "metric-authorization": "who may read /metrics and query Prometheus; the servers' scrapes",
    "http-metrics": "a native origin's request, byte, and storage counters count each request",
    "log-level": "a log level raised through the admin API falls back, or is deleted; refusals",
    "log-redaction": "a token presented to an origin or a cache never reaches its log tail",
    "log-level-xrootd": "an XRootD log level restarts XRootD, which serves again, unless too short",
}

# The scenarios that present no storage token of the session's profile
# (see suites/__init__.py).
ONE_PROFILE = tuple(s for s in SCENARIOS if s not in ("http-metrics", "log-redaction"))


LIVENESS = f"{API}/health"

# An object that init puts in every namespace (init-data.py), which
# `log-redaction` and `log-level-xrootd` read.
SEEDED = "data/0.0"

# The health report's components (metrics/health.go in Pelican): every
# server's web UI; an origin's or a federated cache's advertisements,
# the directors' tests of it, and its registration; and XRootD's, where
# XRootD serves it, and its upkeep of its authorization files.
WEB_UI = "web-ui"
FEDERATED = ("federation", "director", "registry")
XROOTD = ("xrootd", "config-updates")
# The component that watches the host's disks, which warns once one is
# 80% full (Monitoring.StorageWarningThreshold), whatever the federation
# does.
STORAGE = "storage"
# Each status, and pelican_component_health_status's value for it
# (HealthStatusEnum).
STATUS_VALUES = {
    "shutting down": 1.0,
    "critical": 2.0,
    "degraded": 3.0,
    "warning": 4.0,
    "ok": 5.0,
    "unknown": 6.0,
}
# The statuses that may yet change by themselves: a server says
# `warning` of its advertisements and its director tests until the first
# one, and XRootD is `shutting down` while it restarts.
PENDING = ("warning", "unknown", "shutting down")
COMPONENT = "pelican_component_health_status"

# How long every server's health report may take to be all ok; the least
# time to wait before giving up on one that has stopped changing; and how
# often to ask, in seconds. A director tests each server every 15 seconds
# (Director.OriginCacheHealthTestInterval).
HEALTH_WAIT = 150
HEALTH_SETTLE = 35
HEALTH_POLL = 5

# The Prometheus jobs of a server's scrape of itself, and of a director's
# of every origin and cache (web_ui/prometheus.go). Each scrapes every 15
# seconds; how long to wait for every target to be up.
SELF_JOB = "prometheus"
SERVERS_JOB = "origin_cache_servers"
SCRAPE_WAIT = 60

# The counters (metrics/http_metrics.go and metrics/storage_metrics.go).
REQUESTS = "pelican_http_requests_total"
BYTES = "pelican_http_bytes_total"
STORAGE_READS = "pelican_storage_reads_total"
STORAGE_WRITES = "pelican_storage_writes_total"
# The origins whose storage the storage counters count: those served
# through the afero adapter (origin_serve/filesystem.go).
COUNTED_STORAGE = ("posixv2", "pstore")
# How big `http-metrics`'s object is, in bytes.
OBJECT_SIZE = 65536

# How long `log-level`'s first change lasts, in seconds, and how much
# longer the server may take to drop it: it drops a change at its
# deadline (manageLogLevels in logging/level_manager.go).
LEVEL_DURATION = 20
EXPIRY_SLACK = 40
# How long `log-level`'s deleted change, and `log-redaction`'s, last, in
# seconds, were they not deleted.
DELETED_DURATION = 600
REDACTION_DURATION = 60
# The longest change the API takes, in seconds (HandleSetLogLevel).
MAX_DURATION = 86400
# How long a line logged takes to reach the tail, in seconds; it goes
# there as it is logged (LogRingBuffer.Fire), and XRootD's own lines once
# Pelican reads them.
LOG_SETTLE = 3
# What `refusals` asks for, each of which must be refused: a level that
# is none, a parameter that is no setting, one outside Logging, and one
# that is no level. NOT_A_LEVEL is asked for a second, should it be
# taken. It is a string that the server reads only as it starts
# (StartLogRingBuffer in config/log_buffer.go), so a level there does no
# harm; a level in a bool would make every later change of the server's
# configuration fail (param.MultiSet) until it restarts.
BAD_LEVEL = "loud"
NO_PARAMETER = "Logging.NoSuchLevel"
NOT_LOGGING = "Server.WebPort"
NOT_A_LEVEL = "Logging.Buffer.MaxSize"
# logrus's levels, least verbose first, as Pelican names them.
LEVELS = ("panic", "fatal", "error", "warning", "info", "debug", "trace")

# `log-level-xrootd`'s times, in seconds. Its long change outlasts the
# drain period (Xrootd.ShutdownTimeout) by XROOTD_EXTRA, enough to
# outlast the restart and the directors' naming the server again, since
# the scenario then deletes it, which restarts XRootD again.
# DRAIN_DEFAULT is the drain period if the server won't say. XRootD has
# RESTART_WAIT to restart and serve again, and the directors NAMED_WAIT
# to name the server again, since it tells them it is shutting down as
# it restarts. RESTART_POLL is how often to look.
XROOTD_EXTRA = 300
DRAIN_DEFAULT = 2.0
RESTART_WAIT = 120
NAMED_WAIT = 90
RESTART_POLL = 0.5
# XRootD's log level parameters, by server kind (logging/xrootd.go).
XROOTD_PARAMETERS = {"origin": "Logging.Origin.Xrootd", "cache": "Logging.Cache.Xrootd"}

# A Go duration, as a string (time.Duration.String()).
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(h|m|s|ms|us|µs|ns)")
_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9}


def readable(fed: common.Federation) -> str:
    """Return the first exported namespace with an issuer, else the first.

    A namespace has one if it needs a token to read, or takes writes
    (credentials.has_issuer()).
    """
    exported = credentials.exported(fed)
    return next((ns for ns in exported if credentials.has_issuer(fed.exports[ns])), exported[0])


class Test(suite.Test):
    """The federation's servers, and what the scenarios share.

    Its namespace is readable()'s, whose SEEDED object `log-redaction`
    and `log-level-xrootd` read; `http-metrics` writes the run's
    collection in a namespace of its own choosing.

    Attributes:
        web: The admin's and user's tokens, cookies, and logins at each
            server.
        servers: The federation's servers.
        subject: The suite's own subject, in its tokens.
        changes: The IDs of the log level changes that the suite made at
            each server and has not deleted, by server name.
    """

    def __init__(self, session: Session):
        super().__init__(session, "observability", readable(session.fed))
        self.web = webapi.Access(session)
        self.servers = self.web.servers
        self.subject = f"observability-{self.run}"
        self.changes: dict[str, list[str]] = {}

    def health(self, server: Server) -> web.Response:
        """Return server's health report, as its admin reads it."""
        return web.request("GET", server.web_url + HEALTH, token=self.web.admin(server))

    def config(self, server: Server) -> dict[str, Any]:
        """Return server's configuration, as its admin reads it.

        Returns:
            The configuration; {} if reading it fails.
        """
        return web.request("GET", server.web_url + CONFIG, token=self.web.admin(server)).object()

    def caches_at(self, server: Server) -> list[common.Cache]:
        """Return the caches whose web URL is server's."""
        return [c for c in self.fed.caches if c.web_url == server.web_url]

    def origins_at(self, server: Server) -> list[common.Origin]:
        """Return the origins whose web URL is server's.

        Another owner's origins are included.
        """
        found = [o for o in self.fed.origins if o.web_url == server.web_url]
        return found + [w.origin for w in self.fed.owners if w.web_url == server.web_url]

    def xrootd(self, server: Server) -> bool:
        """Return whether XRootD serves an origin or a cache of server's."""
        if self.origins_at(server) and self.fed.origin_variant in common.XROOTD_ORIGINS:
            return True
        return any(c.kind == "xrootd" for c in self.caches_at(server))

    def federated(self, server: Server) -> bool:
        """Return whether server is a federated origin or cache.

        It must be an origin, or a cache that the directors know of, of a
        federation (not a standalone origin).
        """
        if self.fed.standalone:
            return False
        return bool(self.origins_at(server)) or any(not c.site_local for c in self.caches_at(server))

    def web_token(self, server: Server, scopes: tuple[str, ...], key: str | None = None) -> str:
        """Return a token of server's own making, for the suite's subject.

        Args:
            server: The server.
            scopes: Its scopes.
            key: The key to sign it with, if not server's own.
        """
        return credentials.web_token(
            self.fed, server.web_url, self.subject, scopes, credentials.WEB_TOKEN_LIFETIME, key
        )

    def storage_token(self, namespace: str, scopes: tuple[str, ...], key: str = "") -> str:
        """Return a `server` token of the session's profile, for the suite.

        Its claims are as `pelican token create` writes them
        (credentials.server_claims()). Under -p origin-multiuser, it is
        for the subject the origins map to a Unix user; else for the
        suite's own.

        Args:
            namespace: The namespace.
            scopes: Its scopes.
            key: The key to sign it with, if not the origins'.
        """
        key = key or self.fed.origin_key
        subject = credentials.SUBJECT if self.fed.multiuser else self.subject
        claims = credentials.server_claims(
            self.fed, namespace, scopes, subject=subject, profile=self.session.profile
        )
        return jwt.sign(claims, key)


def cookie(test: Test, server: Server) -> dict[str, str]:
    """Return a Cookie header with the admin's login cookie at server."""
    return webapi.cookie_header(login=test.web.cookie(server))


# --------------------------------------------------------------------------
# health-api and health-metrics: GetHealthStatus (metrics/health.go), at
# /api/v1.0/metrics/health (configureMetrics in web_ui/ui.go).


@dataclass
class Verdict:
    """What is wrong with a server's health report.

    Attributes:
        problems: What is wrong.
        pending: Whether any of it may yet change by itself.
        storage_only: Whether the host's disks alone are to blame.
    """

    problems: list[str] = field(default_factory=list[str])
    pending: bool = False
    storage_only: bool = False


def components(report: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """Return each health report component's status and message, by name."""
    found: dict[str, tuple[str, str]] = {}
    for name, item in (common.as_object(report.get("components")) or {}).items():
        item = common.as_object(item) or {}
        found[str(name)] = (str(item.get("status", "")), str(item.get("message", "")))
    return found


def expected_components(test: Test, server: Server) -> tuple[set[str], set[str]]:
    """Return the components server's health report must list, and must not.

    A standalone origin talks to no director or registry
    (standalone_origin_cli_e2e_test.go).
    """
    must = {WEB_UI}
    must_not: set[str] = set()
    if test.fed.standalone:
        must_not.update(FEDERATED)
    elif test.federated(server):
        must.update(FEDERATED)
    if test.xrootd(server):
        must.update(XROOTD)
    return must, must_not


def judge_report(test: Test, server: Server, answer: web.Response) -> Verdict:
    """Return what is wrong with answer, server's health report.

    It must be read (200), say `ok` overall, list every component that
    applies, none that doesn't, and every one `ok`.
    """
    verdict = Verdict()
    if answer.status != 200:
        verdict.problems.append(f"the admin got {answer.describe()}")
        verdict.pending = answer.status is None
        return verdict
    report = answer.object()
    found = components(report)
    must, must_not = expected_components(test, server)
    missing = sorted(must - set(found))
    if missing:
        verdict.problems.append(f"no {', '.join(missing)}")
        verdict.pending = True
    extra = sorted(must_not & set(found))
    if extra:
        verdict.problems.append(f"a standalone origin's report lists {', '.join(extra)}")
    bad = {name: item for name, item in found.items() if item[0] != "ok"}
    for name, (status, message) in sorted(bad.items()):
        verdict.problems.append(f"{name} is {status}" + (f" ({message})" if message else ""))
        verdict.pending = verdict.pending or status in PENDING
    overall = str(report.get("status", ""))
    if overall != "ok":
        verdict.problems.append(f"overall {overall or 'nothing'}")
    elif bad:
        verdict.problems.append("overall ok, despite them")
    verdict.storage_only = bool(bad) and set(bad) == {STORAGE} and not missing and not extra
    return verdict


def settled_reports(test: Test) -> dict[str, web.Response]:
    """Return every server's health report, by server name, once settled.

    They are returned once every one is all ok; or once none may change
    by itself and HEALTH_SETTLE seconds have passed; or after HEALTH_WAIT
    seconds.
    """
    started = time.monotonic()
    while True:
        reports = {s.name: test.health(s) for s in test.servers}
        verdicts = [judge_report(test, s, reports[s.name]) for s in test.servers]
        waited = time.monotonic() - started
        if not any(v.problems for v in verdicts) or waited >= HEALTH_WAIT:
            return reports
        if waited >= HEALTH_SETTLE and not any(v.pending for v in verdicts):
            return reports
        time.sleep(HEALTH_POLL)


def health_access(test: Test, server: Server) -> CaseRow:
    """Check who may read server's health report.

    No one with no credentials (401), not an ordinary user (403), and its
    admin (200), unless Server.HealthMonitoringPublic opens it to all;
    and anyone /api/v1.0/health.
    """
    settings = webapi.server_settings(test.web, server)
    public = settings.get("HealthMonitoringPublic") is True
    url = server.web_url + HEALTH
    user = test.web.user(server)
    case, status, note = judge(
        f"{server.name} access",
        [
            ("none", web.request("GET", url), (200,) if public else (401,)),
            ("user", web.request("GET", url, token=user), (200,) if public else (403,)),
            ("admin", web.request("GET", url, token=test.web.admin(server)), (200,)),
            ("/health", web.request("GET", server.web_url + LIVENESS), (200,)),
        ],
    )
    if public and status == PASS:
        note += " (Server.HealthMonitoringPublic)"
    return case, status, note


def health_api(test: Test) -> list[CaseRow]:
    """Check that each server's health report is all ok, for its admin only.

    Every server's health report, once settled (settled_reports()), must
    be all ok (judge_report()); a report that fails only by its host's
    disks is INCONCLUSIVE. And only the admin may read it
    (health_access()).
    """
    reports = settled_reports(test)

    def check(server: Server) -> list[CaseRow]:
        answer = reports[server.name]
        verdict = judge_report(test, server, answer)
        if verdict.storage_only:
            return [("", INCONCLUSIVE, f"only the host's disks: {first(verdict.problems)}")]
        if verdict.problems:
            return [("", FAIL, "; ".join(verdict.problems))]
        return [("", PASS, f"ok: {', '.join(sorted(components(answer.object())))}")]

    def access(server: Server) -> list[CaseRow]:
        return [health_access(test, server)]

    rows: list[CaseRow] = []
    for server in test.servers:
        rows += each_server([server], check, "components")
        rows += each_server([server], access, "access")
    return rows


def gauges(snapshot: metrics.Snapshot) -> dict[str, float]:
    """Return pelican_component_health_status in snapshot, by component."""
    return {
        dict(labels).get("component", ""): value for labels, value in snapshot.series(COMPONENT).items()
    }


def health_metrics(test: Test) -> list[CaseRow]:
    """Check that each server's health gauges agree with its health report.

    At every server, read the health report, then /metrics, then the
    report again: each component whose status both reports give must
    have pelican_component_health_status of that status's value, and no
    gauge may be of a component that neither report lists, which a
    server alerting on its gauges would take for a fault.
    """

    def check(server: Server) -> list[CaseRow]:
        before = test.health(server)
        snapshot = metrics.fetch(server.web_url)
        after = test.health(server)
        if before.status != 200 or after.status != 200:
            return [("", FAIL, f"the report: {before.describe()}, then {after.describe()}")]
        if snapshot is None:
            return [("", FAIL, "its /metrics did not answer 200 to its own scrape token")]
        then, now = components(before.object()), components(after.object())
        steady = {n: s for n, (s, _) in then.items() if n in now and now[n][0] == s}
        found = gauges(snapshot)
        problems: list[str] = []
        for name, status in sorted(steady.items()):
            want, got = STATUS_VALUES.get(status), found.get(name)
            if got is None:
                problems.append(f"{name} is {status}, but has no gauge")
            elif got != want:
                problems.append(f"{name} is {status} ({want}), but its gauge is {got:g}")
        for name in sorted(set(found) - set(then) - set(now)):
            problems.append(f"a gauge of {found[name]:g} for {name}, which no report lists")
        moved = sorted((set(then) | set(now)) - set(steady))
        note = f"{len(steady)} component(s) agree"
        if moved:
            note += f"; {', '.join(moved)} changed meanwhile"
        return [("", *outcome(problems, note))]

    return each_server(test.servers, check)


# --------------------------------------------------------------------------
# metric-authorization: promMetricAuthHandler and promQueryEngineAuthHandler
# (web_ui/authorization.go), and the scrapes of configDirectorPromScraper
# and ConfigureEmbeddedPrometheus (web_ui/prometheus.go).

# A Prometheus instant query's series: each one's labels and value.
Series = list[tuple[dict[str, str], float]]


def vector(answer: web.Response) -> Series | None:
    """Return the series of a Prometheus instant query's answer.

    Returns:
        The series; None if the answer is not a successful one.
    """
    found = answer.object()
    if answer.status != 200 or found.get("status") != "success":
        return None
    result: Series = []
    for item in common.as_array((common.as_object(found.get("data")) or {}).get("result")) or []:
        item = common.as_object(item) or {}
        labels = {str(k): str(v) for k, v in (common.as_object(item.get("metric")) or {}).items()}
        value = common.as_array(item.get("value")) or []
        try:
            result.append((labels, float(value[1])))
        except (IndexError, TypeError, ValueError):
            continue
    return result


def metrics_access(test: Test, server: Server) -> CaseRow:
    """Check who may read server's /metrics.

    No one with no token (401 or 403); a token of its own issuer with
    scope monitoring.scrape, as it scrapes itself; not one signed by
    another key; the admin's login cookie, which carries the scope
    (setLoginCookie); and not the admin's bearer token, which lacks it
    (403).
    """
    url = server.web_url + "/metrics"
    scrape = test.web_token(server, (credentials.SCRAPE_SCOPE,))
    key, whose = webapi.other_key(test.web, server)
    foreign = test.web_token(server, (credentials.SCRAPE_SCOPE,), key)
    allowed = web.request("GET", url, token=scrape)
    extra: list[str] = []
    if allowed.status == 200 and b"# TYPE " not in allowed.body:
        extra.append("the scrape token's 200 is no Prometheus exposition")
    return judge(
        f"{server.name} metrics",
        [
            ("none", web.request("GET", url), (401, 403)),
            ("scrape token", allowed, (200,)),
            (f"signed by {whose}", web.request("GET", url, token=foreign), (401, 403)),
            ("admin's cookie", web.request("GET", url, headers=cookie(test, server)), (200,)),
            ("admin's token", web.request("GET", url, token=test.web.admin(server)), (403,)),
        ],
        extra,
    )


def promql_access(test: Test, server: Server) -> CaseRow:
    """Check who may query server's Prometheus.

    No one with no token (401 or 403); a token of its own issuer with
    scope monitoring.query, and the admin's login cookie (200); not a
    token with scope monitoring.scrape alone (403).
    """
    promql = f'up{{job="{SELF_JOB}"}}'
    token = test.web_token(server, (credentials.QUERY_SCOPE,))
    scrape = test.web_token(server, (credentials.SCRAPE_SCOPE,))
    answered = webapi.promql(server.web_url, promql, token)
    extra: list[str] = []
    if answered.status == 200 and vector(answered) is None:
        extra.append("the query token's 200 is no successful query")
    return judge(
        f"{server.name} promql",
        [
            ("none", webapi.promql(server.web_url, promql), (401, 403)),
            ("query token", answered, (200,)),
            ("admin's cookie", webapi.promql(server.web_url, promql, headers=cookie(test, server)), (200,)),
            ("scrape token", webapi.promql(server.web_url, promql, scrape), (403,)),
        ],
        extra,
    )


def self_scrape_problems(found: Series | None) -> list[str]:
    """Return what is wrong with a server's `up` for its scrape of itself.

    It must have the one target, up (1).

    Args:
        found: The answer to the server's query (vector()).
    """
    if found is None:
        return ["its query of its own scrape failed"]
    if len(found) != 1:
        return [f"its own scrape has {len(found)} targets, not 1"]
    if found[0][1] != 1:
        return [f"its own scrape is down ({found[0][1]:g})"]
    return []


def servers_scrape_problems(found: Series | None, want: Mapping[tuple[str, str], str]) -> list[str]:
    """Return what is wrong with a director's `up` for the servers' scrape.

    Each of want must be there, and up (1).

    Args:
        found: The answer to the director's query (vector()).
        want: The origins and caches, by type and web host
            (expected_targets()).
    """
    if found is None:
        return ["its query of the servers' scrape failed"]
    values = {(labels.get("server_type", ""), labels.get("instance", "")): value for labels, value in found}
    problems: list[str] = []
    for target in want:
        if target not in values:
            problems.append(f"{target[0]} {target[1]} is not scraped")
        elif values[target] != 1:
            problems.append(f"{target[0]} {target[1]} is down ({values[target]:g})")
    return problems


def scrape_rows(test: Test) -> list[CaseRow]:
    """Check that each server's scrapes are up within SCRAPE_WAIT seconds.

    Each server's scrape of itself must be up (`up` of job prometheus is
    1), which needs its own token (createPromMetricToken) to pass its
    guard; and each director's of every origin and federated cache (job
    origin_cache_servers), which needs the federation's token to pass
    theirs.
    """
    directors = [s for s in test.servers if "director" in s.roles]
    targets = expected_targets(test.fed)

    def problems_of(server: Server) -> list[str]:
        token = test.web_token(server, (credentials.QUERY_SCOPE,))
        problems = self_scrape_problems(
            vector(webapi.promql(server.web_url, f'up{{job="{SELF_JOB}"}}', token))
        )
        if server in directors:
            answer = webapi.promql(server.web_url, f'up{{job="{SERVERS_JOB}"}}', token)
            problems += servers_scrape_problems(vector(answer), targets)
        return problems

    found: dict[str, list[str]] = {}
    deadline = time.monotonic() + SCRAPE_WAIT
    pending: list[Server] = list(test.servers)
    while pending:
        found.update({s.name: problems_of(s) for s in pending})
        pending = [s for s in pending if found[s.name]]
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(HEALTH_POLL)
    rows: list[CaseRow] = []
    for server in test.servers:
        what = f"itself and {len(targets)} origin(s) and cache(s)"
        note = what if server in directors else "itself"
        rows.append((f"{server.name} scrapes", *outcome(found.get(server.name, []), note)))
    return rows


def metric_authorization(test: Test) -> list[CaseRow]:
    """Check Pelican's metric and PromQL authorization, and the scrapes.

    The framework leaves the authorization on. At every server: who may
    read /metrics (metrics_access()) and query Prometheus
    (promql_access()); and its own scrapes, and the directors'
    (scrape_rows()).
    """
    rows = each_server(
        test.servers, lambda server: [metrics_access(test, server), promql_access(test, server)]
    )
    return rows + suite.attempt_cases(functools.partial(scrape_rows, test), (accounts.ApiError,))


# --------------------------------------------------------------------------
# http-metrics: httpMetricsMiddleware (origin_serve/handlers.go), and the
# storage counters of metricsFile (origin_serve/metrics_file.go).


def rose(
    before: metrics.Snapshot, after: metrics.Snapshot, name: str, /, **labels: str
) -> dict[metrics.Labels, float]:
    """Return each series of name with labels that rose, by how much."""
    then = before.series(name, **labels)
    found: dict[metrics.Labels, float] = {}
    for have, value in after.series(name, **labels).items():
        if value > then.get(have, 0):
            found[have] = value - then.get(have, 0)
    return found


def shown(series: Mapping[metrics.Labels, float]) -> str:
    """Return series, by their labels, for a message."""
    if not series:
        return "none"
    return "; ".join(
        "{" + ",".join(f"{k}={v}" for k, v in labels) + f"}} +{value:g}" for labels, value in series.items()
    )


def http_metrics(test: Test) -> list[CaseRow]:
    """Check that the first origin's counters count what the suite sends it.

    At the first origin, served by Pelican, straight: a PUT of
    OBJECT_SIZE bytes, a GET of them, a GET of an object that does not
    exist, and a GET with a token of a key no server knows, each by the
    suite's own subject, which no other request has, so that what rose
    must have risen by exactly these, under multiuser by at least. Its
    pelican_http_requests_total must count each, by method and status,
    the first three with the subject as their username; its
    pelican_http_bytes_total, the bytes in and out; and where its storage
    goes through the afero adapter (posixv2, pstore), its storage writes
    and reads.
    """
    fed = test.fed
    cases = ("put", "get", "missing", "refused")
    if not fed.native_origins:
        why = f"XRootD serves the origins ({fed.origin_variant}), and keeps no such counters"
        return [(c, SKIP, why) for c in cases]
    # A namespace that takes direct writes, and a token to read, so that
    # `refused` can be refused: the protected namespaces first, since
    # /public may take writes (`-p origin-public-writes`) but reads
    # without a token.
    writable = [ns for ns in credentials.exported(fed) if {"Writes", "DirectReads"} <= fed.exports[ns]]
    namespace = next(
        (ns for ns in writable if credentials.has_issuer(fed.exports[ns]) and ns != "public"),
        writable[0] if writable else None,
    )
    if namespace is None:
        return [(c, SKIP, "no namespace takes direct writes and reads") for c in cases]
    public_reads = "PublicReads" in fed.exports[namespace]
    origin = fed.origins[0]
    exact = not fed.multiuser
    username = test.subject if exact else ""
    token = test.storage_token(namespace, RWM)
    unknown = test.storage_token(namespace, R, credentials.UNKNOWN_KEY)
    data = common.digest(test.run.encode()).encode() * (OBJECT_SIZE // 64)
    target = stores.url(origin, namespace, test.rel("http-metrics"))

    before = metrics.fetch(origin.web_url)
    if before is None:
        return [(c, FAIL, f"{origin.svc}'s /metrics did not answer") for c in cases]
    put = web.request("PUT", target, token=token, upload=data)
    got = web.request("GET", target, token=token)
    missing = web.request("GET", f"{target}-missing", token=token)
    refused = web.request("GET", target, token=unknown)
    after = metrics.fetch(origin.web_url)
    if after is None:
        return [(c, FAIL, f"{origin.svc}'s /metrics did not answer afterward") for c in cases]

    def by_user(**labels: str) -> dict[str, str]:
        return {**labels, "username": username} if exact else labels

    def count(case: str, method: str, code: str, problems: list[str]) -> None:
        """Check that the requests counter rose by one for case's request.

        Under multiuser, it may have risen by more.
        """
        series = rose(before, after, REQUESTS, **by_user(server_type="origin", method=method))
        hits = sum(v for labels, v in series.items() if dict(labels).get("code") == code)
        if (hits != 1) if exact else (hits < 1):
            problems.append(
                f"{case}: {REQUESTS} for {method} {code} rose by {hits:g}; of {method}'s,"
                + f" these rose: {shown(series)}"
            )
        elif not exact and not any(dict(k).get("username") for k in series):
            problems.append(f"{case}: no {REQUESTS} series that rose has a username")

    def storage(counter: str, problems: list[str]) -> None:
        """Check that counter rose, if the origins' storage is counted."""
        if fed.origin_variant not in COUNTED_STORAGE:
            return
        series = rose(before, after, counter, **by_user())
        if not series:
            problems.append(f"{counter} did not rise")

    rows: list[CaseRow] = []
    problems: list[str] = []
    if put.status != 201:
        problems.append(f"the PUT got {put.describe()}, not 201")
    count("the PUT", "PUT", "201", problems)
    sent = metrics.delta(before, after, BYTES, **by_user(direction="in", method="PUT"))
    if sent < len(data):
        problems.append(f"{BYTES} in, for PUT, rose by {sent:g}, not {len(data)} or more")
    storage(STORAGE_WRITES, problems)
    rows.append(("put", *outcome(problems, f"{len(data)} bytes in")))

    problems = []
    if got.status != 200 or got.body != data:
        problems.append(f"the GET got {got.describe()}, with {len(got.body)} bytes")
    count("the GET", "GET", "200", problems)
    served = metrics.delta(before, after, BYTES, **by_user(direction="out", method="GET"))
    if served < len(data):
        problems.append(f"{BYTES} out, for GET, rose by {served:g}, not {len(data)} or more")
    storage(STORAGE_READS, problems)
    rows.append(("get", *outcome(problems, f"{served:g} bytes out")))

    problems = []
    if missing.status != 404:
        problems.append(f"the GET of nothing got {missing.describe()}, not 404")
    count("the GET of nothing", "GET", "404", problems)
    rows.append(("missing", *outcome(problems, "counted as 404")))

    if public_reads:
        rows.append(("refused", SKIP, f"reads of /{namespace} take no token to refuse"))
        return rows
    problems = []
    code = str(refused.status)
    if refused.status not in (401, 403):
        problems.append(f"the unknown key's GET got {refused.describe()}, not 401 or 403")
    else:
        series = rose(before, after, REQUESTS, server_type="origin", method="GET", code=code)
        if not series:
            problems.append(f"no {REQUESTS} for GET {code} rose")
    rows.append(("refused", *outcome(problems, f"counted as {code}")))
    return rows


# --------------------------------------------------------------------------
# log-level: HandleSetLogLevel, HandleGetLogLevel, and HandleDeleteLogLevel
# (web_ui/logging_api.go), and LogLevelManager (logging/level_manager.go).


def level_status(test: Test, server: Server) -> dict[str, Any]:
    """Return server's log levels, as its admin reads them.

    Raises:
        suite.SetupError: Reading them fails.
    """
    answer = web.request("GET", server.web_url + LOG_LEVEL, token=test.web.admin(server))
    found = answer.object()
    if answer.status != 200 or "currentLevel" not in found:
        raise suite.SetupError(f"GET {LOG_LEVEL} as the admin: {answer.describe()}")
    return found


def active(status: Mapping[str, Any]) -> dict[str, str]:
    """Return the level of each change a log level status lists, by ID."""
    found: dict[str, str] = {}
    for item in common.as_array(status.get("activeChanges")) or []:
        item = common.as_object(item) or {}
        found[str(item.get("changeId", ""))] = str(item.get("level", ""))
    return found


def raised(level: str) -> str | None:
    """Return a level more verbose than level: debug, or trace from debug.

    Returns:
        That level; None from trace, or from a level Pelican does not
        name.
    """
    if level not in LEVELS or level == "trace":
        return None
    return "trace" if level == "debug" else "debug"


def change_level(
    test: Test,
    server: Server,
    level: str,
    duration: int,
    parameter: str = "",
    token: str | None = None,
) -> web.Response:
    """POST a change of server's log level to level, for duration seconds.

    Args:
        test: The test.
        server: The server whose log level to change.
        level: The level to change to.
        duration: How long the change lasts, in seconds.
        parameter: The log level parameter to change, if not the
            server's own.
        token: The token to present; None for the admin's.
    """
    body: dict[str, Any] = {"level": level, "duration": duration}
    if parameter:
        body["parameterName"] = parameter
    return web.request(
        "POST",
        server.web_url + LOG_LEVEL,
        token=test.web.admin(server) if token is None else token,
        timeout=120,
        json_body=body,
    )


def remember(test: Test, server: Server, change: str) -> None:
    """Note server's log level change, for the suite to delete when done."""
    test.changes.setdefault(server.name, []).append(change)


def made(test: Test, server: Server, answer: web.Response) -> str:
    """Return the ID of the change that answer says server made; "" if none.

    The change is remembered (remember()).
    """
    change = str(answer.object().get("changeId", "")) if answer.status == 200 else ""
    if change:
        remember(test, server, change)
    return change


def delete_change(test: Test, server: Server, change: str) -> web.Response:
    """DELETE server's log level change, as its admin."""
    if change in test.changes.get(server.name, []):
        test.changes[server.name].remove(change)
    return web.request("DELETE", f"{server.web_url}{LOG_LEVEL}/{change}", token=test.web.admin(server))


def undo_changes(test: Test) -> None:
    """Delete every log level change the suite made and has not deleted."""
    for server in test.servers:
        for change in list(test.changes.get(server.name, [])):
            answer = delete_change(test, server, change)
            if answer.status not in (200, 404):
                warn(f"could not delete {server.name}'s log level change: {answer.describe()}")


@dataclass
class Raised:
    """A log level change of `log-level`'s at a server.

    Attributes:
        server: The server.
        before: The level before it.
        level: Its level.
        change: Its ID.
        deadline: When it should be dropped (time.monotonic()).
    """

    server: Server
    before: str
    level: str
    change: str
    deadline: float


def raise_row(test: Test, server: Server) -> tuple[CaseRow, Raised | None]:
    """Raise server's log level for LEVEL_DURATION seconds.

    The POST must get 200 and a change ID, and the level must then be the
    one asked for, with the change listed.

    Returns:
        The row, and the change, if it was made.
    """
    case = f"{server.name} change"
    status = level_status(test, server)
    before = str(status.get("currentLevel"))
    if active(status):
        return (case, INCONCLUSIVE, f"changes already stand: {active(status)}"), None
    level = raised(before)
    if level is None:
        return (case, SKIP, f"its level is {before}, the most verbose"), None
    started = time.monotonic()
    answer = change_level(test, server, level, LEVEL_DURATION)
    change = made(test, server, answer)
    if not change:
        return (case, FAIL, f"the POST got {answer.describe()}, and no change ID"), None
    now = level_status(test, server)
    found = Raised(server, before, level, change, started + LEVEL_DURATION)
    problems: list[str] = []
    if now.get("currentLevel") != level:
        problems.append(f"the level is {now.get('currentLevel')}, not {level}")
    if active(now).get(change) != level:
        problems.append(f"it lists changes {active(now)}, not {change} to {level}")
    return (case, *outcome(problems, f"{before} to {level} for {LEVEL_DURATION}s")), found


def expiry_rows(test: Test, changes: Sequence[Raised]) -> list[CaseRow]:
    """Check that each of changes is dropped by itself, on time.

    Each must be dropped within EXPIRY_SLACK seconds of its deadline: the
    level back to what it was, and the change not listed.
    """
    reverted: dict[str, float] = {}
    last: dict[str, dict[str, Any]] = {}
    pending = list(changes)
    deadline = max((c.deadline for c in changes), default=0) + EXPIRY_SLACK
    while pending:
        time.sleep(2)
        for c in list(pending):
            if time.monotonic() < c.deadline:
                continue
            last[c.server.name] = status = level_status(test, c.server)
            if status.get("currentLevel") == c.before and c.change not in active(status):
                reverted[c.server.name] = time.monotonic() - c.deadline
                pending.remove(c)
        if time.monotonic() >= deadline:
            break
    rows: list[CaseRow] = []
    for c in changes:
        case = f"{c.server.name} expiry"
        if c.server.name in reverted:
            took = max(reverted[c.server.name], 0)
            rows.append((case, PASS, f"back to {c.before} within {took:.0f}s of its deadline"))
            continue
        status = last.get(c.server.name, {})
        rows.append(
            (
                case,
                FAIL,
                f"{EXPIRY_SLACK}s past its deadline, the level is {status.get('currentLevel')},"
                + f" with changes {active(status)}",
            )
        )
    return rows


def delete_row(test: Test, server: Server) -> CaseRow:
    """Check that a change deleted by its ID goes.

    The first DELETE must get 200, the second 404, and the level then be
    back to what it was.
    """
    case = f"{server.name} delete"
    before = str(level_status(test, server).get("currentLevel"))
    level = raised(before) or "info"
    change = made(test, server, change_level(test, server, level, DELETED_DURATION))
    if not change:
        return case, FAIL, "the POST got no change ID"
    first_delete = delete_change(test, server, change)
    second_delete = delete_change(test, server, change)
    status = level_status(test, server)
    extra: list[str] = []
    if change in active(status):
        extra.append(f"the change is still listed: {active(status)}")
    if status.get("currentLevel") != before:
        extra.append(f"the level is {status.get('currentLevel')}, not {before}")
    return judge(case, [("first", first_delete, (200,)), ("second", second_delete, (404,))], extra)


def refusal_row(test: Test, server: Server) -> CaseRow:
    """Check that server's API refuses the changes it must.

    As the admin, a level that is none, a duration over 24 hours
    (MAX_DURATION) or of none, a parameter that is no setting, one that
    is no level, and one outside Logging (400); and a good change, from
    an ordinary user (403), and with no token (401).
    """
    user = test.web.user(server)
    asks: list[tuple[str, str, int, str, str | None, tuple[int, ...]]] = [
        ("bad level", BAD_LEVEL, LEVEL_DURATION, "", None, (400,)),
        ("over 24h", "debug", MAX_DURATION + 1, "", None, (400,)),
        ("no duration", "debug", 0, "", None, (400,)),
        ("no such parameter", "debug", LEVEL_DURATION, NO_PARAMETER, None, (400,)),
        ("not a level", "debug", 1, NOT_A_LEVEL, None, (400,)),
        ("not logging", "debug", LEVEL_DURATION, NOT_LOGGING, None, (400,)),
        ("user", "debug", LEVEL_DURATION, "", user, (403,)),
    ]
    expects: list[webapi.Expect] = []
    for label, level, duration, parameter, token, allowed in asks:
        answer = change_level(test, server, level, duration, parameter, token)
        made(test, server, answer)
        expects.append((label, answer, allowed))
    body = {"level": "debug", "duration": LEVEL_DURATION}
    none = web.request("POST", server.web_url + LOG_LEVEL, json_body=body)
    made(test, server, none)
    expects.append(("none", none, (401,)))
    return judge(f"{server.name} refusals", expects)


def cli_row(test: Test, server: Server) -> CaseRow:
    """Check that `pelican-server server set-logging-level` makes a change.

    `pelican-server server set-logging-level <level> <duration> --server
    <web URL> --token <file>`, with the admin's token, must make a change
    that the API then lists, which the suite deletes.
    """
    case = f"{server.name} cli"
    if test.session.pelican_server is None:
        return case, SKIP, "no pelican-server, which has `server set-logging-level`"
    before = str(level_status(test, server).get("currentLevel"))
    level = raised(before) or "info"
    token_file = test.session.path(f"tokens/observability.admin.{server.name}")
    with open(token_file, "w", encoding="utf-8") as f:
        f.write(test.web.admin(server))
    status, out, err = test.session.server_cmd(
        "server",
        "set-logging-level",
        level,
        f"{LEVEL_DURATION}s",
        "--server",
        server.web_url,
        "--token",
        token_file,
        timeout=120,
    )
    found = re.search(r"Change ID: (\S+)", out)
    if found:
        remember(test, server, found.group(1))
    if status != 0 or not found:
        return case, FAIL, f"exit {status}: {last_line(err or out)}"
    change = found.group(1)
    listed = active(level_status(test, server))
    delete_change(test, server, change)
    if listed.get(change) != level:
        return case, FAIL, f"it printed change {change}, but the API lists {listed}"
    return case, PASS, f"change {change}, to {level}"


def log_level(test: Test) -> list[CaseRow]:
    """Check that each server's log level changes take, and fall back.

    At every server, as its admin: a change of its log level must take
    (raise_row()), and drop by itself at its deadline (expiry_rows());
    one deleted must go (delete_row()); and the API must refuse what it
    should (refusal_row()). The changes run at every server at once. Then
    pelican-server's command at the first server (cli_row()).
    """
    raised_at: list[Raised] = []

    def raise_check(server: Server) -> list[CaseRow]:
        row, change = raise_row(test, server)
        if change is not None:
            raised_at.append(change)
        return [row]

    rows = each_server(test.servers, raise_check)
    rows += suite.attempt_cases(functools.partial(expiry_rows, test, raised_at), (accounts.ApiError,))
    rows += each_server(test.servers, lambda server: [delete_row(test, server)])
    rows += each_server(test.servers, lambda server: [refusal_row(test, server)])
    rows += each_server(test.servers[:1], lambda server: [cli_row(test, server)])
    return rows


# --------------------------------------------------------------------------
# log-redaction: redactEntryCopy (config/logging.go), applied as each line
# enters the log buffer (LogRingBuffer.Fire in config/log_buffer.go), and
# HandleLogTail and LogReadAuthHandler (web_ui/log_buffer_api.go).


def tail(test: Test, server: Server, since: str = "", limit: int = 0) -> dict[str, Any]:
    """Return server's log tail, as its admin reads it.

    Args:
        test: The test.
        server: The server.
        since: The cursor to read after; "" for all the tail holds.
        limit: How many of the newest lines to read; 0 for no limit.

    Raises:
        suite.SetupError: Reading it fails.
    """
    asked = {"since": since} if since else {}
    if limit:
        asked["limit"] = str(limit)
    url = server.web_url + LOG_TAIL + (f"?{urlencode(asked)}" if asked else "")
    answer = web.request("GET", url, token=test.web.admin(server))
    found = answer.object()
    if answer.status != 200 or found.get("enabled") is not True:
        raise suite.SetupError(f"GET {LOG_TAIL} as the admin: {answer.describe()}, {found or 'no JSON'}")
    return found


def tail_access(test: Test, server: Server) -> CaseRow:
    """Check who may read server's log tail.

    No one with no credentials (401), not an ordinary user (403), and its
    admin (200).
    """
    url = f"{server.web_url}{LOG_TAIL}?limit=1"
    return judge(
        f"{server.name} tail-access",
        [
            ("none", web.request("GET", url), (401,)),
            ("user", web.request("GET", url, token=test.web.user(server)), (403,)),
            ("admin", web.request("GET", url, token=test.web.admin(server)), (200,)),
        ],
    )


def present(test: Test, server: Server, token: str) -> list[str]:
    """Present token to server, as a client might, in requests it logs.

    The server logs each request at debug. The token goes in the
    Authorization header and in the query (`authz=Bearer%20<token>`), at
    each of its data URLs, for the SEEDED object; and as a login cookie,
    at its web API.

    Returns:
        The URLs that answered nothing.
    """
    path = test.path(SEEDED)
    urls = [o.url for o in test.origins_at(server)] + [c.url for c in test.caches_at(server)]
    silent: list[str] = []
    for url in urls:
        answers = (
            web.request("GET", url + path, token=token),
            web.request("GET", f"{url}{path}?authz=Bearer%20{token}"),
        )
        silent += [f"{url}{path}" for a in answers if a.status is None]
    login = web.request("GET", server.web_url + CONFIG, headers=webapi.cookie_header(login=token))
    if login.status is None:
        silent.append(server.web_url + CONFIG)
    return silent


def redaction_row(test: Test, server: Server) -> CaseRow:
    """Check that a token presented to server never reaches its log tail.

    With server's log level raised to debug, present a fresh token
    (present()); the log tail since must then hold neither the token nor
    its signature, which Pelican replaces with REDACTED.
    """
    case = f"{server.name} redaction"
    since = str(tail(test, server, limit=1).get("lastCursor", ""))
    answer = change_level(test, server, "debug", REDACTION_DURATION)
    change = made(test, server, answer)
    if not change:
        return case, FAIL, f"raising its level to debug: {answer.describe()}"
    try:
        token = test.storage_token(test.namespace, R)
        silent = present(test, server, token)
        time.sleep(LOG_SETTLE)
        found = tail(test, server, since)
    finally:
        delete_change(test, server, change)
    content = str(found.get("content", ""))
    head, payload, signature = token.split(".")
    if token in content or signature in content:
        lines = [line for line in content.splitlines() if signature in line]
        return case, FAIL, f"the tail holds the token: {(lines or [''])[0][:300]}"
    redacted = content.count(f"{head}.{payload}.REDACTED")
    note = f"{redacted} appearance(s), redacted" if redacted else "the token was not logged"
    if silent:
        return case, INCONCLUSIVE, f"{first(silent)} did not answer; {note}"
    dropped = found.get("dropped")
    if isinstance(dropped, int) and dropped > 0:
        return case, INCONCLUSIVE, f"the tail dropped {dropped} line(s); {note}"
    return case, PASS, f"{len(content.splitlines())} line(s) read; {note}"


def log_redaction(test: Test) -> list[CaseRow]:
    """Check each origin's and cache's log tail, and who may read it.

    At every origin and cache: who may read its log tail (tail_access()),
    and no token presented to it reaching the tail (redaction_row()).
    """
    servers = [s for s in test.servers if test.origins_at(s) or test.caches_at(s)]
    return each_server(servers, lambda server: [tail_access(test, server), redaction_row(test, server)])


# --------------------------------------------------------------------------
# log-level-xrootd: HandleSetLogLevel's drain check, and the restart that
# follows a change of an XRootD log level (handleXrootdLoggingChange in
# xrootd/logging_callback.go, RestartXrootd in xrootd/restart.go).


@dataclass(frozen=True)
class XrootdTarget:
    """An XRootD origin or cache whose XRootD the scenario restarts.

    Attributes:
        kind: Its kind: "origin" or "cache".
        svc: Its service.
        server: Its web API.
        url: The SEEDED object's URL there.
        token: What reads the object; None for no token.
        path: The object's path in the federation.
    """

    kind: str
    svc: str
    server: Server
    url: str
    token: str | None
    path: str

    @property
    def parameter(self) -> str:
        """Its XRootD log level parameter."""
        return XROOTD_PARAMETERS[self.kind]

    @property
    def route(self) -> str:
        """The directors' route to it: `origin`, or `object` for a cache."""
        return "origin" if self.kind == "origin" else "object"


def xrootd_targets(test: Test) -> list[XrootdTarget]:
    """Return the scenario's targets.

    They are the first origin, if XRootD serves the origins, and the
    first federated XRootD cache.
    """
    fed = test.fed
    found: list[tuple[str, common.Origin | common.Cache]] = []
    if fed.origins and fed.origin_variant in common.XROOTD_ORIGINS:
        found.append(("origin", fed.origins[0]))
    cache = next((c for c in fed.federated_caches if c.kind == "xrootd"), None)
    if cache is not None:
        found.append(("cache", cache))
    path = test.path(SEEDED)
    token = test.setup if credentials.has_issuer(test.caps) else None
    return [
        XrootdTarget(kind, s.svc, test.web.server_at(s.web_url), s.url + path, token, path)
        for kind, s in found
    ]


def duration_seconds(value: Any) -> float | None:
    """Return a duration from a server's configuration, in seconds.

    Go's JSON gives a time.Duration in nanoseconds, and a string may be a
    Go duration (e.g. 1m30s).

    Returns:
        The seconds, or None if value is neither.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1e9
    if isinstance(value, str):
        parts: list[tuple[str, str]] = _DURATION.findall(value)
        if parts and "".join(n + u for n, u in parts) == value:
            return sum(float(n) * _UNITS[u] for n, u in parts)
    return None


def setting(config: Mapping[str, Any], name: str) -> Any:
    """Return setting name of a server's configuration, or None if unset.

    The name is dotted, e.g. Xrootd.ShutdownTimeout.
    """
    found: Any = config
    for part in name.split("."):
        found = (common.as_object(found) or {}).get(part)
    return found


@dataclass
class Watch:
    """What `log-level-xrootd` saw of a restart.

    Attributes:
        down: When the object was first not served, in seconds into the
            watch.
        back: When it was served again, in seconds into the watch.
        xrootd_down: Whether the health report said XRootD was not ok.
        xrootd_ok: Whether it said ok when last asked.
    """

    down: float | None = None
    back: float | None = None
    xrootd_down: bool = False
    xrootd_ok: bool = False

    @property
    def restarted(self) -> bool:
        """Whether a restart was seen."""
        return self.xrootd_down or self.down is not None

    @property
    def serving(self) -> bool:
        """Whether the object is served as before, if it ever was not."""
        return self.down is None or self.back is not None


def watch_restart(test: Test, target: XrootdTarget, served: int | None) -> Watch:
    """Watch target, up to RESTART_WAIT seconds, as XRootD restarts.

    It sees when the object is first not answered as before, and when it
    is again; and whether the health report says XRootD is not ok, then
    ok. RestartXrootd says `shutting down` for the drain period, so a
    look every RESTART_POLL seconds sees it.

    Args:
        test: The test.
        target: The target.
        served: The status of a GET of the object before the change.
    """
    seen = Watch()
    started = time.monotonic()
    while time.monotonic() - started < RESTART_WAIT:
        answer = web.request("GET", target.url, token=target.token, timeout=5)
        now = time.monotonic() - started
        if answer.status != served and seen.down is None:
            seen.down = now
        elif answer.status == served and seen.down is not None and seen.back is None:
            seen.back = now
        status = components(test.health(target.server).object()).get("xrootd", ("", ""))[0]
        seen.xrootd_ok = status == "ok"
        seen.xrootd_down = seen.xrootd_down or status not in ("", "ok")
        if seen.restarted and seen.serving and seen.xrootd_ok:
            break
        time.sleep(RESTART_POLL)
    return seen


def restart_problems(seen: Watch) -> tuple[list[str], str]:
    """Return what is wrong with a restart that watch_restart() saw.

    It must have happened, and XRootD must serve again.

    Returns:
        The problems, and the outage, for a note.
    """
    problems: list[str] = []
    if not seen.restarted:
        problems.append(f"no restart was seen in {RESTART_WAIT}s")
    elif not seen.serving:
        problems.append(f"it did not serve again within {RESTART_WAIT}s")
    elif not seen.xrootd_ok:
        problems.append(f"the health report did not say XRootD was ok within {RESTART_WAIT}s")
    if seen.down is not None and seen.back is not None:
        outage = f"out for {seen.back - seen.down:.1f}s, from {seen.down:.1f}s in"
    else:
        outage = "no outage of the object was seen"
    return problems, outage


def named_problems(test: Test, target: XrootdTarget) -> list[str]:
    """Wait up to NAMED_WAIT seconds until every director names target.

    Returns:
        What is still wrong after that.
    """
    host = director.host(target.url)

    def named() -> list[str]:
        return director.naming(test.fed.directors, target.route, target.path, host)

    if common.wait_for(lambda: len(named()) == len(test.fed.directors), NAMED_WAIT, 2):
        return []
    return [f"after {NAMED_WAIT}s, only {named() or 'no director'} named {host}"]


def xrootd_rows(test: Test, target: XrootdTarget) -> list[CaseRow]:
    """Check that a change of target's XRootD log level restarts XRootD.

    At target, as its admin: a change of its XRootD log level for its
    drain period (Xrootd.ShutdownTimeout) or less must be refused (400),
    since XRootD restarts to take it, after draining, and the change
    would revert first. A longer one must get 200, requiresRestart, and
    effectiveAt; XRootD must restart and serve the object again, and
    every director name it again. Deleting the change restarts XRootD
    once more, after which the same must hold.
    """
    name = target.svc
    config = test.config(target.server)
    drain = duration_seconds(setting(config, "Xrootd.ShutdownTimeout"))
    drain = DRAIN_DEFAULT if drain is None else drain
    current = str(setting(config, target.parameter) or "").lower()
    level = "info" if current == "debug" else "debug"
    short = max(1, int(drain))
    refused = change_level(test, target.server, level, short, target.parameter)
    made(test, target.server, refused)
    rows = [judge(f"{name} short", [(f"{short}s, draining {drain:g}s", refused, (400,))])]
    if refused.status == 200:
        # It restarts XRootD after all, which must finish before another
        # change can restart it (RestartXrootd refuses to overlap).
        watch_restart(test, target, web.request("GET", target.url, token=target.token).status)

    served = web.request("GET", target.url, token=target.token).status
    if served is None:
        return rows + [(f"{name} restart", FAIL, "before the change, the object went unanswered")]
    duration = int(drain) + XROOTD_EXTRA
    answer = change_level(test, target.server, level, duration, target.parameter)
    change = made(test, target.server, answer)
    found = answer.object()
    case = f"{name} restart"
    if not change:
        return rows + [(case, FAIL, f"the POST got {answer.describe()}, and no change ID")]
    problems: list[str] = []
    if found.get("requiresRestart") is not True or not found.get("effectiveAt"):
        problems.append(f"it says requiresRestart {found.get('requiresRestart')!r}")
    seen = watch_restart(test, target, served)
    trouble, outage = restart_problems(seen)
    rows.append((case, *outcome(problems + trouble, f"{target.parameter} to {level}; {outage}")))
    rows.append((f"{name} named", *outcome(named_problems(test, target), "by every director")))

    deleted = delete_change(test, target.server, change)
    case = f"{name} restored"
    if deleted.status != 200:
        rows.append((case, FAIL, f"deleting the change got {deleted.describe()}"))
        return rows
    seen = watch_restart(test, target, served)
    trouble, outage = restart_problems(seen)
    trouble += named_problems(test, target)
    rows.append((case, *outcome(trouble, f"back to {current or 'its default'}; {outage}")))
    return rows


def log_level_xrootd(test: Test) -> list[CaseRow]:
    """Check XRootD's log level where it serves the origins or a cache.

    It is checked at the first of each (xrootd_rows()).
    """
    targets = xrootd_targets(test)
    if not targets:
        return [("", SKIP, "XRootD serves no origin or federated cache here")]
    rows: list[CaseRow] = []
    for target in targets:
        found = suite.attempt_cases(functools.partial(xrootd_rows, test, target), (accounts.ApiError,))
        for case, status, note in found:
            rows.append((case or target.svc, status, note))
    return rows


# --------------------------------------------------------------------------


RUN: dict[str, Callable[[Test], list[CaseRow]]] = {
    "health-api": health_api,
    "health-metrics": health_metrics,
    "metric-authorization": metric_authorization,
    "http-metrics": http_metrics,
    "log-level": log_level,
    "log-redaction": log_redaction,
    "log-level-xrootd": log_level_xrootd,
}


def skip(_fed: common.Federation) -> str | None:
    """Return None: every server reports on itself."""
    return None


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, and clean up after them.

    It deletes the log level changes they left, and removes what
    `http-metrics` put.
    """
    test = Test(session)
    try:
        for name in selected:
            found = suite.attempt_cases(functools.partial(RUN[name], test), (accounts.ApiError,))
            suite.record(results, name, found)
    finally:
        undo_changes(test)
        test.web.wait_for_logins()
        test.clean("after the run")
