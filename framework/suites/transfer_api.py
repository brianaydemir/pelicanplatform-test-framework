"""The transfer API, which runs transfer jobs on the server.

This covers its authentication, the storage credentials it keeps for
each user, third-party copies through it, the jobs it reports, and what
it confines jobs to. It is origin-0's (`-p origin-transfer-api`), or a
transfer server's own (`-p topo-transfer-server`), in a process of its
own: generated/transfer-api says which (common.Federation.transfer_api).

Its own tokens carry the pelican.transfer scope, from its host's
issuer, which the suite mints with that host's key: origin-0's, or the
transfer server's local issuer; storage credentials are the server's
tokens for /protected-a. Objects go under /protected-a/data/xfer/<run>/;
the suite removes them, and the credentials it added, afterward. A job
of origin-0's API may read or write the origin's own filesystem only
through an export, so `local-get` and `local-put` check that a job is
refused one that leaves the exports; a transfer server confines a job
to nothing (RegisterTransferAPI in Pelican's transfer/transfer.go), so
there they check that such a job runs, and moves the bytes it names,
and `path-not-allowed` that a job no export holds is taken and then
fails.

The API's edges come after its main paths: tokens whose subjects are
no usernames (`subjects`), a finished job cancelled (`cancel-finished`),
a job's event stream (`events`), sources on the origin's own network and
a prefix that merely begins like an export (`ssrf-and-prefixes`), and
`pelican transfer credential` (`credential-cli`). Two need configuration
that no shape has: `enabled-groups` (Transfer.EnabledGroups, a group of
the run's own) and `oauth-clients` (Transfer.EnableOAuth2Clients), for
which the suite restarts the API's host once with both, as the last of
its configuration layers (control.restart_with()), and restores it; and
`restart`, which restarts the host while a job runs. Each restart needs
a control helper and a topology other than `tiny`, whose origin is the
whole federation's process; elsewhere those rows skip, and say why.
"""

import contextlib
import glob
import json
import os
import re
import secrets
import time
import traceback
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Optional, Union

from testlib import (
    accounts,
    blocks,
    common,
    control,
    credentials,
    director,
    issuer,
    stores,
    web,
)
from testlib.accounts import Api
from testlib.common import die, first, warn
from testlib.issuer import User
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "ping": "the public ping answers that the service is the transfer service",
    "auth-none": "a request without a token is refused",
    "auth-storage-token": "a storage token, without pelican.transfer, is refused",
    "auth-unknown-key": "a pelican.transfer token signed by a key no server knows is refused",
    "credentials": "a storage credential is listed, never shown, and its name is its own",
    "copy": (
        "`object copy --transfer-server ... --wait`: a third-party copy, byte for byte; and"
        " `--dest-origin`, which finds origin-0's transfer API, or none"
    ),
    "job-status": "a finished job reports completed, and is listed",
    "bad-dest-credential": "a copy with a read-only destination credential fails, and writes nothing",
    "unknown-credential": "a job naming a credential that does not exist is refused",
    "other-user": "another user can neither see a job nor use a credential",
    "path-not-allowed": "a job outside the origin's exports is refused",
    "cancel": "cancelling an unknown job is not found; a finished one stays finished",
    "local-get": "a job may not download to the origin's own filesystem outside an export",
    "local-put": "a job may not upload from the origin's own filesystem",
    "subjects": "tokens whose subjects are a URL, a character, 65 characters, or `admin`: taken",
    "cancel-finished": "cancelling a job that failed keeps it failed",
    "events": "a job's event stream reports its status until it ends; another user's is not found",
    "ssrf-and-prefixes": (
        "a source on loopback or link-local, and a prefix that merely begins like an export,"
        " are refused; no credential's token is in the database in plain"
    ),
    "credential-cli": (
        "`pelican transfer credential add`, `list`, and `delete` agree with the API,"
        " and show no token"
    ),
    "enabled-groups": (
        "with Transfer.EnabledGroups, the issuer grants pelican.transfer to a member alone,"
        " whose token and login cookie the API takes (restarts origin-0)"
    ),
    "oauth-clients": (
        "OAuth2 clients are off by default; on, a token exchange bootstraps a credential"
        " that jobs use, and that the server refreshes (restarts origin-0)"
    ),
    "restart": (
        "a job that a restart of origin-0 interrupts fails, saying so; a credential"
        " survives; the restart forgets a finished job, which a cancel must leave as it was"
    ),
}

# The scenarios that need the API's host configured otherwise (see
# run_configured()), and the one that restarts it.
CONFIGURED = ("enabled-groups", "oauth-clients")
RESTARTS = "restart"

# The transfer API's scope, and its routes.
SCOPE = "pelican.transfer"
API = "/api/v1.0/transfer"
# How long a job may take, in seconds.
JOB_WAIT = 120
# The jobs' terminal states.
FINISHED = ("completed", "failed", "cancelled")
# A key that no server knows.
UNKNOWN_KEY = "test-keys/unknown.pem"
# Its other routes.
OAUTH_CLIENTS = "/oauth-clients"
BOOTSTRAP = "/credentials/bootstrap/token-exchange"
EVENT_STREAM = {"Accept": "text/event-stream"}
# The run's configuration of a server, as the last of its layers
# (control.restart_with()), and how long the suite waits afterward until
# every director names origin-0 again and the API answers.
CONFIG_FILE = "90-transfer-api.yaml"
SETTLE_WAIT = 150
# `subjects`: each case's subject. A URL, one character, and 65 characters
# are no usernames (ValidateIdentifier in database/identifiers.go);
# `admin` is the built-in user's.
SUBJECTS = (
    ("url", "http://cilogon.org/serverA/users/1234"),
    ("short", "x"),
    ("long", "u" * 65),
    ("admin", "admin"),
)
# `ssrf-and-prefixes`: sources on the origin's own network, which
# Server.SSRFProtection.AllowedCIDRs (config.d/base/40-origin.yaml) does
# not allow, though it allows the private ranges.
SSRF_SOURCES = (
    ("link-local", "http://169.254.169.254/latest/meta-data/"),
    ("loopback", "http://127.0.0.1:8444/api/v1.0/health"),
)
# The grants and scopes of the OAuth2 client that `oauth-clients`
# registers with the transfer API: the token exchange (RFC 8693) that
# bootstraps a credential, and the refresh grant that renews it.
EXCHANGE_GRANTS = f"{issuer.EXCHANGE_GRANT} {issuer.REFRESH_GRANT}"
CREDENTIAL_SCOPES = ("storage.read:/", "storage.create:/", "storage.modify:/", "offline_access")
# `restart`: Origin.TransferRateLimit, so that a copy of the source object
# is still under way when origin-0 restarts: a posixv2 origin reads and
# writes it through one bucket (origin_serve/rate_limited_fs.go), so 8 MiB
# takes about a minute. Other backends are not slowed.
RATE_LIMIT = "256KB/s"
# What a job interrupted by a restart must say (transfer/recovery.go).
INTERRUPTED = "interrupted by transfer server restart"


def skip(fed: common.Federation) -> Optional[str]:
    """Return why there is no API, or nowhere to copy to, or None."""
    if fed.transfer_api is None:
        return "the origins run no transfer API ('-p origin-transfer-api')"
    if "Writes" not in fed.exports.get("protected-a", ()):
        return "/protected-a takes no writes, so no copy can land"
    return None


class ApiError(Exception):
    """A refusal by the API of what a scenario's setup asked of it."""


# A row: its case (appended to the scenario's name, if any), its status,
# and its note. A scenario returns its status and note, or its rows.
Row = tuple[str, str, str]
Rows = Union[tuple[str, str], list[Row]]


def outcome(problems: Sequence[str], note: str) -> Row:
    """Return a row with no case: FAIL with problems, or PASS with note.

    A FAIL's note is the first of problems.
    """
    return ("", FAIL, first(problems)) if problems else ("", PASS, note)


class Test:
    """The API, its tokens and credentials, and the run's objects.

    Attributes:
        url: The URL of the API's host.
        issuer: The issuer of the API's own tokens.
        key_dir: The key directory of the API's host.
        storage: A storage token for /protected-a, for credentials.
        read_only: A read-only one.
        token: The user's token for the API.
        data: The object that the jobs copy.
        credential_ids: The credentials added, each with the token that
            owns it.
        confined: Whether the API confines jobs to the origin's exports:
            origin-0's does, a transfer server's does not.
        web: The web URL of the API's host: origin-0 (`fed` in the tiny
            topology), or the transfer server.
        svc: The host's service, which `restart` restarts.
        key: The host's key.
        origin_web: The web URL of origin-0, whose namespace issuer the
            OAuth2 clients are of, and which `restart` slows: the API's
            host, or another server.
        origin_svc: The service of origin-0.
        origin_key: The key of origin-0.
        issuer_clients: The issuer's clients that the configured
            scenarios made, each with the web URL and the admin token
            that made it, to remove afterward.
        user_ids: The users they made, to remove afterward.
        group_ids: The groups they made, to remove afterward.
        oauth_client_ids: The OAuth2 clients they registered, to remove
            afterward.
        secrets: The secrets that `ssrf-and-prefixes` must find nowhere
            in the database of the API's host in plain, beyond the
            storage tokens.
    """

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        api = self.fed.transfer_api
        if api is None:
            die("framework/var/generated/transfer-api says the transfer API is off")
        self.url, self.issuer, self.key_dir = api.url, api.issuer, api.key_dir
        self.base = f"data/xfer/{session.run}"
        self.storage = session.token("protected-a")
        self.read_only = session.token("protected-a", credentials.R)
        self.token = self.mint("user", common.signing_key(self.key_dir), credentials.SUBJECT)
        # Past pstore's chunk, so that a copy takes many reads and writes.
        self.data = os.urandom(blocks.CHUNK_UNDECLARED + 4321)
        self.credential_ids: list[tuple[str, str]] = []
        self._finished_job: Optional[dict[str, Any]] = None
        self.confined = api.confined
        self.web = self.url
        self.svc = api.svc
        self.key = common.signing_key(self.key_dir)
        self.origin_web = self.fed.origin_web_url
        self.origin_svc = self.fed.origins[0].svc
        self.origin_key = common.signing_key(f"issuer-keys/{self.origin_svc}")
        self._admin: Optional[str] = None
        self._origin_admin: Optional[str] = None
        self.issuer_clients: list[tuple[str, str, str, issuer.Client]] = []
        self.user_ids: list[str] = []
        self.group_ids: list[str] = []
        self.oauth_client_ids: list[str] = []
        self.secrets: list[str] = []

    def mint(self, name: str, key: str, subject: str) -> str:
        """Return a token for the API, signed with key, for subject.

        name names the file it is kept in.
        """
        path = self.session.path(f"tokens/transfer-api.{name}")
        credentials.mint(
            self.fed,
            path,
            "/protected-a/",
            key,
            self.issuer,
            "/",
            credentials.LONG,
            (),
            subject,
            raw_scopes=(SCOPE,),
        )
        with open(path, encoding="utf-8") as f:
            return f.read().strip()

    def admin_token(self) -> str:
        """Return a token the API's host's web API takes as its admin's.

        See credentials.mint_admin().
        """
        if self._admin is None:
            self._admin = credentials.mint_admin(
                self.fed,
                self.session.path("tokens/transfer-api.admin"),
                credentials.local_issuer(self.fed, self.web),
                self.key,
            )
        return self._admin

    def origin_admin_token(self) -> str:
        """Return a token that origin-0's web API takes as its admin's.

        It is admin_token() where origin-0 is the API's host.
        """
        if self.origin_web == self.web:
            return self.admin_token()
        if self._origin_admin is None:
            self._origin_admin = credentials.mint_admin(
                self.fed,
                self.session.path("tokens/transfer-api.origin-admin"),
                credentials.local_issuer(self.fed, self.origin_web),
                self.origin_key,
            )
        return self._origin_admin

    def admin(self) -> Api:
        """Return the API's host's web API, as its admin."""
        return Api(self.web, self.admin_token())

    def token_file(self, name: str, token: str) -> str:
        """Write token to a file, for the client's --token.

        Returns:
            The file's path.
        """
        path = self.session.path(f"tokens/transfer-api.{name}.file")
        with open(path, "w", encoding="utf-8") as f:
            f.write(token)
        return path

    def issuer_client(
        self,
        namespace: str,
        grants: Sequence[str],
        allowed_scopes: Sequence[str] = issuer.CLIENT_SCOPES,
    ) -> issuer.Client:
        """Create and return a client of an issuer, as the server's admin.

        The issuer is namespace's, origin-0's, or the API's host's local
        issuer (issuer.LOCAL_NAMESPACE). The client is allowed grants and
        allowed_scopes, and removed when the suite ends.
        """
        if namespace == issuer.LOCAL_NAMESPACE:
            site, token = self.web, self.admin_token()
        else:
            site, token = self.origin_web, self.origin_admin_token()
        made = issuer.create_client(
            site,
            namespace,
            token,
            grants,
            allowed_scopes,
            name=f"pelican-test-framework transfer-api {self.session.run}",
        )
        self.issuer_clients.append((site, namespace, token, made))
        return made

    def call(
        self,
        method: str,
        path: str,
        token: Optional[str],
        body: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[int], dict[str, Any], bytes]:
        """Send method to the API's path, with body as JSON if given.

        Returns:
            The status, the answer as a JSON object ({} if it is not one),
            and the raw answer.
        """
        upload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        answer = web.request(
            method, f"{self.url}{API}{path}", token=token, upload=upload, headers=headers
        )
        try:
            found = common.as_object(json.loads(answer.body)) or {}
        except ValueError:
            found = {}
        return answer.status, found, answer.body

    def rel(self, name: str) -> str:
        """Return where object name of the run is in /protected-a."""
        return f"{self.base}/{name}"

    def url_of(self, name: str) -> str:
        """Return the federation URL of object name of the run."""
        return f"{self.fed.url}/protected-a/{self.rel(name)}"

    def held(self, name: str) -> list[Optional[str]]:
        """Return what each store holds at object name.

        See stores.held_anywhere().
        """
        return stores.held_anywhere(
            self.fed.origins, "protected-a", self.rel(name), {"protected-a": self.storage}
        )

    def add_credential(self, name: str, token: str, owner: Optional[str] = None) -> str:
        """Add a storage credential holding token, and return its ID.

        It is the user's, or owner's, a token of another subject.

        Raises:
            ApiError: The API refused it.
        """
        status, found, raw = self.call(
            "POST", "/credentials", owner or self.token, {"name": name, "access_token": token}
        )
        if status != 201 or not found.get("id"):
            raise ApiError(f"could not add credential {name}: HTTP {status}: {raw[:200]!r}")
        self.credential_ids.append((str(found["id"]), owner or self.token))
        return str(found["id"])

    def submit(
        self, body: dict[str, Any], token: Optional[str] = None
    ) -> tuple[Optional[int], dict[str, Any]]:
        """Submit a job, as the user unless token says otherwise.

        Returns:
            The status, and the answer as a JSON object.
        """
        status, found, _ = self.call("POST", "/jobs", token or self.token, body)
        return status, found

    def wait(self, job_id: str, token: Optional[str] = None) -> dict[str, Any]:
        """Wait until the job is finished, and return its status.

        It asks as the user, or as token's, and gives up after JOB_WAIT
        seconds, returning the status as it stands.
        """
        deadline = time.monotonic() + JOB_WAIT
        while True:
            _, found, _ = self.call("GET", f"/jobs/{job_id}", token or self.token)
            if found.get("status") in FINISHED or time.monotonic() >= deadline:
                return found
            time.sleep(2)

    def copied(
        self, name: str, source_id: str, dest_id: str, token: Optional[str] = None
    ) -> Optional[str]:
        """Copy the source object to object name of the run, through a job.

        The job uses the credentials, as the user or as token's, and is
        waited for.

        Returns:
            Why it did not complete, with the copy at some origin, or None
            if it did.
        """
        status, found = self.submit(self.copy_job("src", name, source_id, dest_id), token)
        if status != 201 or not found.get("job_id"):
            return f"submitting a copy to {name}: HTTP {status}: {found}"
        job = self.wait(str(found["job_id"]), token)
        if job.get("status") != "completed":
            return f"the copy to {name}: {job.get('status')} ({str(job.get('error'))[:120]})"
        if common.digest(self.data) not in self.held(name):
            return f"the copy to {name} completed, but no origin holds it"
        return None

    def copy_job(self, source: str, dest: str, source_id: str, dest_id: str) -> dict[str, Any]:
        """Return a job copying object source to object dest of the run."""
        return {
            "transfers": [
                {
                    "operation": "copy",
                    "source": self.url_of(source),
                    "destination": self.url_of(dest),
                }
            ],
            "source_credential_id": source_id,
            "dest_credential_id": dest_id,
        }

    def finished_job(self) -> dict[str, Any]:
        """Return the status of a copy through the API that has finished.

        The copy is made once.

        Raises:
            ApiError: The API refused the copy.
        """
        if self._finished_job is None:
            writer = self.add_credential(f"{self.session.run}-job", self.storage)
            status, found = self.submit(self.copy_job("src", "job-copy", writer, writer))
            if status != 201 or not found.get("job_id"):
                raise ApiError(f"could not submit a copy: HTTP {status}: {found}")
            self._finished_job = self.wait(str(found["job_id"]))
            self._finished_job.setdefault("job_id", found["job_id"])
        return self._finished_job


def refused(status: Optional[int], found: dict[str, Any]) -> Optional[str]:
    """Return why an answer is not a refusal to authenticate, or None."""
    if status not in (401, 403) or found.get("code") != "UNAUTHORIZED":
        return f"HTTP {status}, {found.get('code')}, not 401 or 403 UNAUTHORIZED"
    return None


def ping(test: Test) -> tuple[str, str]:
    """Check the public ping."""
    status, found, _ = test.call("GET", "/ping", None)
    if status != 200 or found.get("service") != "transfer":
        return FAIL, f"HTTP {status}: {found}"
    return PASS, str(found.get("issuer", ""))


def auth_with(test: Test, token: Optional[str]) -> tuple[str, str]:
    """Check that listing jobs with token is refused."""
    status, found, _ = test.call("GET", "/jobs", token)
    why = refused(status, found)
    if why:
        return FAIL, why
    return PASS, f"HTTP {status}"


def auth_unknown_key(test: Test) -> tuple[str, str]:
    """Check that a pelican.transfer token of an unknown key is refused."""
    return auth_with(test, test.mint("unknown", UNKNOWN_KEY, credentials.SUBJECT))


def credentials_scenario(test: Test) -> tuple[str, str]:
    """Check that a credential is listed, never shown, and its name its own.

    It is listed by its ID, no answer may show its token, and a second
    one by the same name must be a conflict.
    """
    name = f"{test.session.run}-listed"
    status, found, raw = test.call(
        "POST", "/credentials", test.token, {"name": name, "access_token": test.storage}
    )
    if status != 201 or not found.get("id"):
        return FAIL, f"adding it: HTTP {status}: {raw[:200]!r}"
    test.credential_ids.append((str(found["id"]), test.token))
    shown = [raw]
    status, _, raw = test.call("GET", "/credentials", test.token)
    shown.append(raw)
    if status != 200 or str(found["id"]).encode() not in raw:
        return FAIL, f"listing them: HTTP {status}, without {found['id']}"
    status, _, raw = test.call("GET", f"/credentials/{found['id']}", test.token)
    shown.append(raw)
    if status != 200:
        return FAIL, f"getting it: HTTP {status}"
    if any(test.storage.encode() in answer for answer in shown):
        return FAIL, "an answer shows the credential's token"
    status, _, _ = test.call(
        "POST", "/credentials", test.token, {"name": name, "access_token": test.storage}
    )
    if status != 409:
        return FAIL, f"adding it again: HTTP {status}, not 409"
    return PASS, str(found["id"])


def copy(test: Test) -> list[Row]:
    """Check `pelican object copy` through the transfer API, waiting for it.

    It names the API with `--transfer-server` (`server`); and then names
    origin-0 with `--dest-origin` instead, which asks the origin whether
    it runs the transfer API (pingTransferService in cmd/object_copy.go)
    and submits the copy there if it does (`dest-origin`): where
    origin-0's is the API, the copy must land; where a transfer server's
    is, the command must fail, saying that the service is not available
    there, and land nothing.
    """
    writer = test.add_credential(f"{test.session.run}-cli", test.storage)
    token_file = test.session.path("tokens/transfer-api.user.file")
    with open(token_file, "w", encoding="utf-8") as f:
        f.write(test.token)
    common_args = [
        "--transfer-server-token",
        token_file,
        "--source-credential-id",
        writer,
        "--dest-credential-id",
        writer,
        "--wait",
        "--json",
        test.url_of("src"),
    ]
    rows: list[Row] = []
    code, out, err = test.session.pelican_cmd(
        "object", "copy", "--transfer-server", test.url, *common_args, test.url_of("cli-copy")
    )
    if code != 0:
        rows.append(("server", FAIL, f"exit {code}: {last_line(err or out)}"))
    elif common.digest(test.data) not in test.held("cli-copy"):
        rows.append(("server", FAIL, "no origin holds the copy"))
    else:
        rows.append(("server", PASS, "the copy landed"))
    code, out, err = test.session.pelican_cmd(
        "object",
        "copy",
        "--dest-origin",
        test.origin_web,
        *common_args,
        test.url_of("dest-origin-copy"),
    )
    landed = common.digest(test.data) in test.held("dest-origin-copy")
    if test.origin_web == test.web:
        if code != 0:
            rows.append(("dest-origin", FAIL, f"exit {code}: {last_line(err or out)}"))
        elif not landed:
            rows.append(("dest-origin", FAIL, "exit 0, but no origin holds the copy"))
        else:
            rows.append(("dest-origin", PASS, "found origin-0's transfer API; the copy landed"))
    elif code == 0:
        rows.append(("dest-origin", FAIL, "exit 0, though origin-0 runs no transfer API"))
    elif landed:
        rows.append(("dest-origin", FAIL, f"exit {code}, but the copy landed anyway"))
    elif "not available" not in err + out:
        rows.append(
            (
                "dest-origin",
                FAIL,
                f"exit {code}, not for want of the service: {last_line(err or out)}",
            )
        )
    else:
        rows.append(("dest-origin", PASS, f"exit {code}: {last_line(err or out)}"))
    return rows


def job_status(test: Test) -> tuple[str, str]:
    """Check a finished copy's status, and the list of the user's jobs."""
    job = test.finished_job()
    if job.get("status") != "completed" or not job.get("completed_at"):
        return FAIL, f"the job: {job}"
    if common.digest(test.data) not in test.held("job-copy"):
        return FAIL, "the job completed, but no origin holds the copy"
    status, found, _ = test.call("GET", "/jobs?status=completed", test.token)
    listed = [common.as_object(j) or {} for j in common.as_array(found.get("jobs")) or []]
    if status != 200 or job["job_id"] not in [j.get("job_id") for j in listed]:
        return FAIL, f"listing completed jobs: HTTP {status}, without {job['job_id']}"
    return PASS, str(job["job_id"])


def bad_dest_credential(test: Test) -> tuple[str, str]:
    """Check that a copy whose destination credential may only read fails.

    It is accepted, and fails, writing nothing.
    """
    writer = test.add_credential(f"{test.session.run}-src", test.storage)
    reader = test.add_credential(f"{test.session.run}-read-only", test.read_only)
    status, found = test.submit(test.copy_job("src", "bad-dest", writer, reader))
    if status != 201 or not found.get("job_id"):
        return FAIL, f"submitting it: HTTP {status}: {found}"
    job = test.wait(str(found["job_id"]))
    if job.get("status") != "failed" or not job.get("error"):
        return FAIL, f"the job: {job}, not failed with an error"
    if any(test.held("bad-dest")):
        return FAIL, "the job failed, but an origin holds the copy"
    return PASS, f"failed: {str(job['error'])[:120]}"


def unknown_credential(test: Test) -> tuple[str, str]:
    """Check a job naming a credential that does not exist."""
    missing = str(uuid.uuid4())
    status, found = test.submit(test.copy_job("src", "unknown", missing, missing))
    if status != 400 or found.get("code") != "INVALID_REQUEST":
        return FAIL, f"HTTP {status}, {found.get('code')}, not 400 INVALID_REQUEST"
    return PASS, "HTTP 400"


def other_user(test: Test) -> tuple[str, str]:
    """Check that another user (subject) sees no job of the user's.

    Nor may they use the user's credentials.
    """
    job = test.finished_job()
    other = test.mint("other", common.signing_key(test.key_dir), "pelican-test-other")
    status, _, _ = test.call("GET", f"/jobs/{job['job_id']}", other)
    if status != 404:
        return FAIL, f"getting the user's job: HTTP {status}, not 404"
    status, _, raw = test.call("GET", "/jobs", other)
    if status != 200 or str(job["job_id"]).encode() in raw:
        return FAIL, f"listing jobs: HTTP {status}, or it lists the user's job"
    status, _, _ = test.call("DELETE", f"/jobs/{job['job_id']}", other)
    if status != 404:
        return FAIL, f"cancelling the user's job: HTTP {status}, not 404"
    writer = test.credential_ids[0][0]
    status, found = test.submit(test.copy_job("src", "other", writer, writer), other)
    if status != 400:
        return FAIL, f"using the user's credential: HTTP {status}, not 400: {found}"
    return PASS, ""


def unconfined(test: Test, body: dict[str, Any], what: str) -> tuple[str, str]:
    """Check that a job no export holds is taken, and then fails.

    A transfer server confines jobs to nothing, so it must take the job
    (201); the job must then fail, since nothing serves what it names,
    and never complete. what labels the job in the verdict's note.
    """
    status, found = test.submit(body)
    if status != 201 or not found.get("job_id"):
        return FAIL, (
            f"{what}: HTTP {status}, {found.get('code')}, not 201 from a transfer server,"
            " which confines jobs to no export"
        )
    job = test.wait(str(found["job_id"]))
    if job.get("status") != "failed":
        return FAIL, f"{what}: taken, then {job.get('status')}, not failed"
    return PASS, f"{what}: taken, then failed: {str(job.get('error'))[:80]}"


def path_not_allowed(test: Test) -> tuple[str, str]:
    """Check a copy between paths that no export of the origin holds.

    It must be refused by origin-0's API, and taken and then failed by a
    transfer server's.
    """
    elsewhere = f"{test.fed.url}/elsewhere/{test.rel('x')}"
    body = {
        "transfers": [
            {"operation": "copy", "source": elsewhere, "destination": f"{elsewhere}-2"}
        ]
    }
    if not test.confined:
        return unconfined(test, body, "/elsewhere")
    status, found = test.submit(body)
    if status != 403 or found.get("code") != "PATH_NOT_ALLOWED":
        return FAIL, f"HTTP {status}, {found.get('code')}, not 403 PATH_NOT_ALLOWED"
    return PASS, "HTTP 403"


def cancel(test: Test) -> tuple[str, str]:
    """Check cancelling a job that does not exist, then a finished one.

    The finished one must stay as it finished.
    """
    status, _, _ = test.call("DELETE", f"/jobs/{uuid.uuid4()}", test.token)
    if status != 404:
        return FAIL, f"cancelling an unknown job: HTTP {status}, not 404"
    job = test.finished_job()
    status, _, raw = test.call("DELETE", f"/jobs/{job['job_id']}", test.token)
    if status is None or not (200 <= status < 300 or status == 409):
        return FAIL, f"cancelling a completed job: HTTP {status}, not 2xx or 409: {raw[:120]!r}"
    _, found, _ = test.call("GET", f"/jobs/{job['job_id']}", test.token)
    if found.get("status") != job.get("status"):
        return FAIL, f"cancelling a {job.get('status')} job made it {found.get('status')}"
    return PASS, f"HTTP {status}"


def confined(
    test: Test, transfer: dict[str, Any], landed: Callable[[], Optional[str]], expected: str
) -> tuple[str, str]:
    """Check a job of one transfer that leaves the exports.

    The transfer is on the API's host's own filesystem. landed() is the
    SHA-256 of what it moved, if anything, and expected what that would
    be. Origin-0's API confines jobs to its exports, so it must refuse
    the job, or fail it, and landed() find nothing. A transfer server
    confines jobs to nothing: there the job must complete, and landed()
    find the bytes it named.
    """
    writer = test.add_credential(f"{test.session.run}-{transfer['operation']}", test.storage)
    body = {
        "transfers": [transfer],
        "source_credential_id": writer,
        "dest_credential_id": writer,
    }
    status, found = test.submit(body)
    if not test.confined:
        if status != 201 or not found.get("job_id"):
            return FAIL, (
                f"refused (HTTP {status}, {found.get('code')}), though a transfer server"
                " confines jobs to no export"
            )
        job = test.wait(str(found["job_id"]))
        if job.get("status") != "completed":
            return FAIL, (
                f"taken, then {job.get('status')}, though a transfer server confines jobs to"
                f" no export: {str(job.get('error'))[:120]}"
            )
        moved = landed()
        if moved is None:
            return FAIL, "completed, but what it moved is not there"
        if moved != expected:
            return FAIL, "completed, but what it moved is not the bytes it named"
        return PASS, "taken and completed, as a transfer server confines jobs to no export"
    if status == 201 and found.get("job_id"):
        job = test.wait(str(found["job_id"]))
        if job.get("status") == "completed" or landed() is not None:
            return FAIL, f"the job ran: {job.get('status')}, and wrote outside the exports"
        return PASS, f"accepted, then {job.get('status')}"
    if landed() is not None:
        return FAIL, f"refused (HTTP {status}), but something was written"
    return PASS, f"refused (HTTP {status}, {found.get('code')})"


def local_get(test: Test) -> tuple[str, str]:
    """Check a `get` to the API's host's filesystem, outside every export.

    The destination is origin-0's /data, its store's mount, which no
    export names as is, or the transfer server's /var/lib/pelican, its
    state. Origin-0's API must not write the file there; a transfer
    server's must, and the file must be the source object.
    """
    name = f"local-get-{test.session.run}"
    if test.confined:
        # Every origin-0 mounts framework/var/data/origin/0 at /data.
        destination, landed = f"/data/{name}", f"data/origin/0/{name}"
    else:
        # The transfer server mounts framework/var/state/transfer at
        # /var/lib/pelican (docker-compose.yaml).
        destination, landed = f"/var/lib/pelican/{name}", f"state/{test.svc}/{name}"
    transfer = {
        "operation": "get",
        "source": test.url_of("src"),
        "destination": destination,
    }
    try:
        return confined(test, transfer, lambda: common.sha256(landed), common.digest(test.data))
    finally:
        try:
            os.remove(landed)
        except OSError:
            pass


def local_put(test: Test) -> tuple[str, str]:
    """Check a `put` from a file of the API's host's own, /etc/hostname.

    Docker writes it as the container's hostname and a newline.
    Origin-0's API must not land it; a transfer server's must, as the
    file.
    """
    transfer = {
        "operation": "put",
        "source": "/etc/hostname",
        "destination": test.url_of("local-put"),
    }
    return confined(
        test,
        transfer,
        lambda: next((d for d in test.held("local-put") if d), None),
        common.digest(f"{test.svc}\n".encode()),
    )


# --------------------------------------------------------------------------
# The API's edges.


def subjects(test: Test) -> list[Row]:
    """Check tokens whose subjects are no usernames, or `admin`'s.

    They are pelican.transfer tokens whose subjects are a URL, one
    character, or 65 characters, or the built-in `admin`'s: each must list
    its jobs (200), run a copy of its own, and see no other subject's job.
    The API resolves each token's issuer and subject to a user of its own
    (GetOrCreateUser in database/collection.go, from TransferAuthMiddleware
    in transfer/auth.go).
    """
    rows: list[Row] = []
    for case, subject in SUBJECTS:
        token = test.mint(f"subject-{case}", test.key, subject)
        status, found, _ = test.call("GET", "/jobs", token)
        if status != 200:
            what = f"{found.get('code')}: {str(found.get('error'))[:80]}"
            rows.append((case, FAIL, f"GET /jobs as {subject!r}: HTTP {status}, {what}"))
            continue
        problems: list[str] = []
        writer = test.add_credential(f"{test.session.run}-{case}", test.storage, owner=token)
        why = test.copied(f"subject-{case}", writer, writer, token)
        if why:
            problems.append(why)
        status, listed, _ = test.call("GET", "/jobs", token)
        jobs = [common.as_object(j) or {} for j in common.as_array(listed.get("jobs")) or []]
        mine = [str(j.get("job_id")) for j in jobs]
        if status != 200 or not mine:
            problems.append(f"the subject's jobs: HTTP {status}, listing {len(mine)}")
        elif case != "admin":
            # The user's own token is for another subject, so it must see
            # none of these; `admin`'s record is whoever the API takes the
            # built-in user to be, which the user is not either.
            status, _, _ = test.call("GET", f"/jobs/{mine[0]}", test.token)
            if status != 404:
                problems.append(f"another subject's GET of the job: HTTP {status}, not 404")
        rows.append(
            (case, *outcome(problems, f"{subject[:40]!r}: lists, copies, and owns")[1:])
        )
    return rows


def cancel_finished(test: Test) -> tuple[str, str]:
    """Check that cancelling a job that failed keeps it failed.

    The job fails since its destination credential may only read. The
    cancel is taken (2xx) or refused as too late (409), and the job must
    stay `failed`. Pelican's CancelJob counts every transfer of a failed
    job as cancelled, and the handler then records the job cancelled
    (client_agent/transfer_manager.go, transfer/handlers.go).
    """
    stamp = test.session.run
    writer = test.add_credential(f"{stamp}-cancel-src", test.storage)
    reader = test.add_credential(f"{stamp}-cancel-ro", test.read_only)
    status, found = test.submit(test.copy_job("src", "cancel-failed", writer, reader))
    if status != 201 or not found.get("job_id"):
        return FAIL, f"submitting the copy: HTTP {status}: {found}"
    job_id = str(found["job_id"])
    job = test.wait(job_id)
    if job.get("status") != "failed":
        return FAIL, f"the job: {job.get('status')}, not failed"
    status, _, raw = test.call("DELETE", f"/jobs/{job_id}", test.token)
    if status is None or not (200 <= status < 300 or status == 409):
        return FAIL, f"cancelling it: HTTP {status}, not 2xx or 409: {raw[:120]!r}"
    _, after, _ = test.call("GET", f"/jobs/{job_id}", test.token)
    if after.get("status") != "failed":
        return FAIL, f"cancelling a failed job (HTTP {status}) made it {after.get('status')}"
    return PASS, f"HTTP {status}, still failed"


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Return the events in a text/event-stream body.

    Comments (keepalives) are passed over.

    Returns:
        Each event's kind (`message` if it names none) and its data, as a
        JSON object ({} if it is not one).
    """
    parsed: list[tuple[str, dict[str, Any]]] = []
    kind = ""
    data: list[str] = []
    for line in text.splitlines() + [""]:
        if line == "":
            if data:
                try:
                    found = common.as_object(json.loads("\n".join(data))) or {}
                except ValueError:
                    found = {}
                parsed.append((kind or "message", found))
            kind, data = "", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            kind = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:") :].strip())
    return parsed


def events(test: Test) -> tuple[str, str]:
    """Check a job's event stream (GET /jobs/<id>/events).

    With Accept: text/event-stream, it must be `status` events whose data
    names the job, the last of them its terminal status, after which the
    stream ends (StreamJobEvents in client_agent/job_events.go). For
    another user, the stream must not be found (404).
    """
    writer = test.add_credential(f"{test.session.run}-events", test.storage)
    status, found = test.submit(test.copy_job("src", "events", writer, writer))
    if status != 201 or not found.get("job_id"):
        return FAIL, f"submitting the copy: HTTP {status}: {found}"
    job_id = str(found["job_id"])
    answer = web.request(
        "GET",
        f"{test.url}{API}/jobs/{job_id}/events",
        token=test.token,
        headers=EVENT_STREAM,
        timeout=JOB_WAIT,
    )
    if answer.status != 200:
        return FAIL, f"the stream: {answer.describe()}"
    if not answer.header("Content-Type").startswith("text/event-stream"):
        return FAIL, f"the stream is typed {answer.header('Content-Type')!r}"
    seen = parse_sse(answer.body.decode(errors="replace"))
    if not seen:
        return FAIL, f"no event in {len(answer.body)} bytes of stream"
    kinds = {kind for kind, _ in seen}
    if kinds != {"status"}:
        return FAIL, f"events of kinds {sorted(kinds)}, not status alone"
    others = [str(data.get("job_id")) for _, data in seen if data.get("job_id") != job_id]
    if others:
        return FAIL, f"an event names job {others[0]!r}, not {job_id}"
    last = str(seen[-1][1].get("status"))
    if last not in FINISHED:
        return FAIL, f"the stream ended with status {last!r}, not a terminal one"
    if last != "completed":
        return FAIL, f"the job ended {last}: {str(seen[-1][1].get('error'))[:120]}"
    _, job, _ = test.call("GET", f"/jobs/{job_id}", test.token)
    if job.get("status") != last:
        return FAIL, f"the stream ended {last}, but the job is {job.get('status')}"
    other = test.mint("events-other", test.key, "pelican-test-other")
    refused_answer = web.request(
        "GET", f"{test.url}{API}/jobs/{job_id}/events", token=other, headers=EVENT_STREAM
    )
    if refused_answer.status != 404:
        return FAIL, f"another user's stream of the job: {refused_answer.describe()}, not 404"
    return PASS, f"{len(seen)} status event(s), ending {last}; another user: 404"


def database_files(test: Test) -> list[str]:
    """Return the API's host's database files, under framework/var/state."""
    return sorted(glob.glob(f"state/{test.svc}/*.sqlite*"))


def ssrf_and_prefixes(test: Test) -> list[Row]:
    """Check SSRF and prefix refusals, and that the database hides tokens.

    A job whose source is on loopback or link-local, which no allowed
    range covers, must be refused (403, ENDPOINT_NOT_ALLOWED); one whose
    source and destination are under a prefix that merely begins like an
    export, 403 PATH_NOT_ALLOWED, since a prefix matches whole path
    segments (transferMatchesPrefixes in transfer/handlers.go). And no
    credential's token may lie in the API's host's database in plain,
    which keeps each encrypted (transfer/encryption.go).
    """
    rows: list[Row] = []
    problems: list[str] = []
    for case, source in SSRF_SOURCES:
        body = {
            "transfers": [
                {"operation": "copy", "source": source, "destination": test.url_of("ssrf")}
            ]
        }
        status, found = test.submit(body)
        if status != 403 or found.get("code") != "ENDPOINT_NOT_ALLOWED":
            problems.append(
                f"{case} ({source}): HTTP {status}, {found.get('code')}, not 403"
                + " ENDPOINT_NOT_ALLOWED"
            )
    rows.append(("endpoints", *outcome(problems, "loopback and link-local: 403")[1:]))
    sibling = f"{test.fed.url}/protected-ab/{test.rel('x')}"
    body = {
        "transfers": [{"operation": "copy", "source": sibling, "destination": f"{sibling}-2"}]
    }
    if not test.confined:
        rows.append(("prefix", *unconfined(test, body, "/protected-ab")))
    else:
        status, found = test.submit(body)
        if status != 403 or found.get("code") != "PATH_NOT_ALLOWED":
            rows.append(
                (
                    "prefix",
                    FAIL,
                    f"HTTP {status}, {found.get('code')}, not 403 PATH_NOT_ALLOWED",
                )
            )
        else:
            rows.append(("prefix", PASS, "/protected-ab: 403"))
    # The tokens that the suite stored, and any client secret it
    # registered, as bytes, since the database holds strings as is.
    secrets_held = [test.storage, test.read_only, *test.secrets]
    files = database_files(test)
    if not files:
        rows.append(
            ("secrets", INCONCLUSIVE, f"no database under framework/var/state/{test.svc}")
        )
        return rows
    leaked: list[str] = []
    for path in files:
        try:
            with open(path, "rb") as f:
                content = f.read()
        except OSError as e:
            rows.append(("secrets", INCONCLUSIVE, f"cannot read framework/var/{path}: {e}"))
            return rows
        leaked += [
            f"{os.path.basename(path)} holds a token in plain"
            for secret in secrets_held
            if secret.encode() in content
        ]
    rows.append(("secrets", *outcome(leaked, f"{len(files)} file(s), no token in plain")[1:]))
    return rows


def cli_json(out: str) -> Any:
    """Return the JSON a `pelican transfer` command printed, or None."""
    try:
        return json.loads(out.strip() or "null")
    except ValueError:
        return None


def credential_cli(test: Test) -> tuple[str, str]:
    """Check `pelican transfer credential add`, `list`, and `delete`.

    They run with the user's token in a file, against the API's host
    (cmd/transfer_credential.go): each must agree with the API, and
    `list` must show no token.
    """
    token_file = test.token_file("cli-user", test.token)
    name = f"{test.session.run}-cli"
    common_args = ["--server", test.url, "--token", token_file, "--json"]
    code, out, err = test.session.pelican_cmd(
        "transfer",
        "credential",
        "add",
        "--name",
        name,
        "--access-token-file",
        test.session.token_file("protected-a"),
        "--issuer",
        test.fed.issuer_of("protected-a"),
        *common_args,
    )
    if code != 0:
        return FAIL, f"add: exit {code}: {last_line(err or out)}"
    made = common.as_object(cli_json(out)) or {}
    cred_id = str(made.get("id") or "")
    if not cred_id or made.get("name") != name:
        return FAIL, f"add printed no credential named {name}: {last_line(out)}"
    test.credential_ids.append((cred_id, test.token))
    if test.storage in out + err:
        return FAIL, "add printed the token"
    status, found, _ = test.call("GET", f"/credentials/{cred_id}", test.token)
    if status != 200 or found.get("name") != name:
        return FAIL, f"the API's GET of what add made: HTTP {status}, {found.get('name')!r}"
    code, out, err = test.session.pelican_cmd("transfer", "credential", "list", *common_args)
    if code != 0:
        return FAIL, f"list: exit {code}: {last_line(err or out)}"
    listed = [common.as_object(c) or {} for c in common.as_array(cli_json(out)) or []]
    if not any(c.get("id") == cred_id and c.get("name") == name for c in listed):
        return FAIL, f"list does not show {name} ({cred_id}): {last_line(out)}"
    if test.storage in out + err:
        return FAIL, "list printed the token"
    code, out, err = test.session.pelican_cmd(
        "transfer", "credential", "delete", cred_id, *common_args
    )
    if code != 0:
        return FAIL, f"delete: exit {code}: {last_line(err or out)}"
    status, _, _ = test.call("GET", f"/credentials/{cred_id}", test.token)
    if status != 404:
        return FAIL, f"the API's GET of what delete removed: HTTP {status}, not 404"
    test.credential_ids.remove((cred_id, test.token))
    return PASS, f"{cred_id}: added, listed without its token, deleted"


# --------------------------------------------------------------------------
# The scenarios that need the API's host configured otherwise: a group of
# the run's own in Transfer.EnabledGroups, and Transfer.EnableOAuth2Clients.


def unconfigurable(test: Test) -> Optional[str]:
    """Return why the API's host cannot be restarted, or None if it can.

    It is restarted with configuration of the run's own, or while a job
    runs.
    """
    if test.fed.tiny:
        return "the tiny topology's origin is the whole federation's process"
    return control.unavailable()


def transfer_group(test: Test) -> str:
    """Return the group that the run puts in Transfer.EnabledGroups."""
    return f"xfer-{test.session.run}"


def configured_text(test: Test) -> str:
    """Return the run's configuration of the API's host."""
    group = json.dumps(transfer_group(test))
    return f"Transfer:\n  EnabledGroups: [{group}]\n  EnableOAuth2Clients: true\n"


def settle(test: Test) -> Optional[str]:
    """Wait until the API answers, and the directors name origin-0 again.

    Each director, where there are any, must name it for the source
    object.

    Returns:
        Why that did not happen within SETTLE_WAIT seconds, or None.
    """
    fed = test.fed
    path = f"/protected-a/{test.rel('src')}"
    host = director.host(fed.origins[0].url)
    found: list[str] = []

    def ready() -> bool:
        found.clear()
        status, _, _ = test.call("GET", "/ping", None)
        if status != 200:
            found.append(f"the API's ping got HTTP {status}")
            return False
        named = director.naming(fed.directors, "origin", path, host)
        if len(named) != len(fed.directors):
            found.append(f"only {named} named {host}")
            return False
        return True

    if not common.wait_for(ready, SETTLE_WAIT, interval=5):
        return f"after {SETTLE_WAIT}s, {first(found) if found else 'origin-0 is not back'}"
    return None


def run_configured(test: Test, names: list[str], results: common.Results) -> None:
    """Run the scenarios named with the API's host configured for them.

    The host is restarted with the run's configuration, and then
    restored, waiting each time until the federation serves as before.
    """
    try:
        with control.restart_with(test.svc, {CONFIG_FILE: configured_text(test)}):
            why = settle(test)
            if why:
                for name in names:
                    results.add(f"{name} configured", FAIL, f"once origin-0 restarted, {why}")
                return
            for name in names:
                record(results, name, guarded(CONFIGURED_RUN[name], test))
    except control.ControlError as e:
        for name in names:
            results.add(f"{name} configured", FAIL, str(e))
        return
    why = settle(test)
    if why:
        results.add("restored", FAIL, f"once origin-0 was restored, {why}")


def log_in(test: Test, username: str, user_id: str) -> str:
    """Log username in, and return the login cookie.

    The password is the run's, set through an invite the admin mints.
    The cookie carries the groups they are in.
    """
    invite = accounts.password_invite(test.admin(), user_id)
    password = secrets.token_urlsafe(12)
    accounts.set_password(test.web, invite, password)
    return accounts.login(test.web, username, password)


def enabled_groups(test: Test) -> list[Row]:
    """Check that with Transfer.EnabledGroups, only a member gets the API.

    With it naming a group of the run's, the API's host's local issuer
    grants pelican.transfer to a member who asks, through the device flow
    approved with their login cookie, and not to another user
    (transferAccessAllowed in oauth2/issuer/handlers.go); the member's token
    is taken at the API (200), the other's refused; and the member's login
    cookie is taken too, since the API takes a cookie whose groups match
    (TransferAuthMiddleware in transfer/auth.go), while the other's is
    refused (403).
    """
    admin = test.admin()
    accounts.agree_to_aup(admin)
    group = transfer_group(test)
    gid = accounts.create_group(admin, group)
    test.group_ids.append(gid)
    users = {
        "member": f"xfer-member-{test.session.run}",
        "other": f"xfer-other-{test.session.run}",
    }
    cookies: dict[str, str] = {}
    for role, username in users.items():
        made = accounts.must(
            admin.call("POST", accounts.USERS, {"username": username}), f"creating {username}"
        )
        user_id = str(made.object.get("id", ""))
        test.user_ids.append(user_id)
        token = credentials.mint_user(
            test.fed,
            test.session.path(f"tokens/transfer-api.web.{role}"),
            credentials.local_issuer(test.fed, test.web),
            test.key,
            username,
        )
        accounts.agree_to_aup(Api(test.web, token))
        if role == "member":
            accounts.add_member(admin, gid, user_id)
        cookies[role] = log_in(test, username, user_id)
    client = test.issuer_client(
        issuer.LOCAL_NAMESPACE, (issuer.DEVICE_GRANT,), ("openid", SCOPE)
    )
    rows: list[Row] = []
    tokens: dict[str, str] = {}
    problems: list[str] = []
    for role, username in users.items():
        try:
            tokens[role] = issuer.token(client, User(cookie=cookies[role]), ("openid", SCOPE))
        except issuer.IssuerError as e:
            problems.append(f"{username}'s device flow: {e}")
            continue
        granted = SCOPE in issuer.scopes(tokens[role])
        if granted != (role == "member"):
            problems.append(
                f"{users[role]}'s token {'has' if granted else 'lacks'} {SCOPE}:"
                + f" {sorted(issuer.scopes(tokens[role]))}"
            )
    rows.append(("issuer", *outcome(problems, f"{SCOPE} to the member alone")[1:]))
    problems = []
    for role, want in (("member", (200,)), ("other", (401, 403))):
        if role not in tokens:
            continue
        status, _, _ = test.call("GET", "/jobs", tokens[role])
        if status not in want:
            problems.append(f"GET /jobs with {users[role]}'s token: HTTP {status}, not {want}")
    rows.append(("token", *outcome(problems, "the member's taken, the other's refused")[1:]))
    problems = []
    for role, want in (("member", (200,)), ("other", (403,))):
        answer = web.request(
            "GET", f"{test.url}{API}/jobs", headers={"Cookie": f"login={cookies[role]}"}
        )
        if answer.status not in want:
            problems.append(
                f"GET /jobs with {users[role]}'s login cookie: {answer.describe()}, not {want}"
            )
    rows.append(("cookie", *outcome(problems, "the member's taken, the other's refused")[1:]))
    return rows


def oauth_clients_disabled(test: Test) -> list[Row]:
    """Check that the OAuth2 client routes are off by default.

    With Transfer.EnableOAuth2Clients off, as by default, they refuse
    everyone (403, DISABLED).
    """
    problems: list[str] = []
    body = {
        "name": "off",
        "issuer_url": test.fed.issuer_of("protected-a"),
        "client_id": "off",
        "client_secret": "off",  # nosec B105: no secret, since the route is off
    }
    for method, sent in (("GET", None), ("POST", body)):
        status, found, _ = test.call(method, OAUTH_CLIENTS, test.token, sent)
        if status != 403 or found.get("code") != "DISABLED":
            problems.append(f"{method}: HTTP {status}, {found.get('code')}, not 403 DISABLED")
    return [("disabled", *outcome(problems, "GET and POST: 403 DISABLED")[1:])]


def expiry_of(found: dict[str, Any]) -> Optional[datetime]:
    """Return a credential's token_expiry, as a time, or None."""
    text = str(found.get("token_expiry") or "")
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    text = re.sub(r"[Zz]$", "+00:00", text)
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def oauth_clients(test: Test) -> list[Row]:
    """Check OAuth2 clients, with Transfer.EnableOAuth2Clients on.

    A client of the namespace issuer's, which the admin creates for the
    token exchange and the refresh grant, is registered with the API
    (POST /oauth-clients, 201), listed without its secret, and deleted
    (`crud`); through it, a token the admin got from the issuer is
    exchanged for a credential (POST /credentials/bootstrap/token-exchange,
    201), which a copy then uses (`bootstrap`; handleTokenExchangeBootstrap
    in transfer/bootstrap.go); and under short tokens, a copy once the
    credential's access token has expired must still complete, since the
    server refreshes it first (`refresh`; getDecryptedAccessToken in
    transfer/credentials.go).
    """
    issuer_url = test.fed.issuer_of("protected-a")
    # Under the external issuer, the origin's admin can make no client
    # of it: `crud` registers made-up credentials, and the rest skip.
    external = test.fed.external_issuer or not test.fed.embedded_issuer
    exchange: Optional[issuer.Client] = None
    device: Optional[issuer.Client] = None
    client_id, client_secret = f"made-up-{test.session.run}", secrets.token_urlsafe(24)
    if not external:
        exchange = test.issuer_client(
            "/protected-a", (issuer.EXCHANGE_GRANT, issuer.REFRESH_GRANT)
        )
        device = test.issuer_client("/protected-a", (issuer.DEVICE_GRANT,))
        client_id, client_secret = exchange.client_id, exchange.client_secret
    test.secrets.append(client_secret)
    rows: list[Row] = []
    problems: list[str] = []
    body = {
        "name": f"{test.session.run}-exchange",
        "issuer_url": issuer_url,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_types": EXCHANGE_GRANTS,
        "scopes": " ".join(CREDENTIAL_SCOPES),
    }
    status, found, raw = test.call("POST", OAUTH_CLIENTS, test.token, body)
    registered = str(found.get("id") or "")
    if status != 201 or not registered:
        return [("crud", FAIL, f"registering the client: HTTP {status}: {raw[:200]!r}")]
    test.oauth_client_ids.append(registered)
    shown = [raw]
    status, _, raw = test.call("GET", OAUTH_CLIENTS, test.token)
    shown.append(raw)
    if status != 200 or registered.encode() not in raw:
        problems.append(f"listing the clients: HTTP {status}, without {registered}")
    if any(client_secret.encode() in answer for answer in shown):
        problems.append("an answer shows the client's secret")
    status, _, _ = test.call("POST", OAUTH_CLIENTS, test.token, body)
    if status != 409:
        problems.append(f"registering it again: HTTP {status}, not 409")
    rows.append(
        ("crud", *outcome(problems, f"{registered}: registered, listed, no secret")[1:])
    )
    if device is None:
        skipped = "the exports trust the external issuer, whose clients the origin cannot make"
        return rows + [("bootstrap", SKIP, skipped), ("refresh", SKIP, skipped)]

    try:
        subject = issuer.token(device, User(token=test.admin_token()), CREDENTIAL_SCOPES)
    except issuer.IssuerError as e:
        return rows + [("bootstrap", FAIL, f"the admin's token from the issuer: {e}")]
    body = {
        "subject_token": subject,
        "issuer_url": issuer_url,
        "name": f"{test.session.run}-bootstrapped",
        "scopes": " ".join(CREDENTIAL_SCOPES),
    }
    status, found, raw = test.call("POST", BOOTSTRAP, test.token, body)
    cred_id = str(found.get("id") or "")
    if status != 201 or not cred_id:
        return rows + [("bootstrap", FAIL, f"HTTP {status}: {raw[:300]!r}")]
    test.credential_ids.append((cred_id, test.token))
    first_expiry = expiry_of(found)
    why = test.copied("bootstrapped", cred_id, cred_id)
    if why:
        rows.append(("bootstrap", FAIL, why))
    else:
        rows.append(("bootstrap", PASS, f"credential {cred_id}, whose copy landed"))

    lifetimes = test.fed.issuer_lifetimes
    if lifetimes.access > 300:
        rows.append(("refresh", SKIP, "access tokens last an hour ('-p origin-short-tokens')"))
        return rows
    if first_expiry is None:
        rows.append(("refresh", FAIL, "the credential names no token_expiry"))
        return rows
    wait = first_expiry.timestamp() + 3 - time.time()
    if wait > 0:
        print(f"Waiting {wait:.0f}s until the credential's access token has expired ...")
        time.sleep(wait)
    why = test.copied("refreshed", cred_id, cred_id)
    if why:
        rows.append(("refresh", FAIL, f"once the access token had expired, {why}"))
        return rows
    _, found, _ = test.call("GET", f"/credentials/{cred_id}", test.token)
    later = expiry_of(found)
    if later is None or later <= first_expiry:
        rows.append(("refresh", FAIL, f"the copy landed, but token_expiry is {later}"))
        return rows
    rows.append(("refresh", PASS, "the copy landed, with a refreshed token"))
    return rows


CONFIGURED_RUN: dict[str, Callable[[Test], Rows]] = {
    "enabled-groups": enabled_groups,
    "oauth-clients": oauth_clients,
}


# --------------------------------------------------------------------------
# restart


def restart(test: Test) -> list[Row]:
    """Check jobs and credentials across a restart of the API's host.

    The host restarts while a copy runs (origin-0 slowed, where it can
    be): the job must then be `failed`, saying it was interrupted by a
    restart (reconcileInterruptedJobs in transfer/recovery.go), or, if
    the copy finished first, the row is inconclusive. A credential stored
    before the restart must still work (`credential`). And a job that
    finished before the restart, which the manager no longer holds,
    cancelled, must keep its status (`forgotten`): Pelican records it
    cancelled instead (handleCancelTransferJob in transfer/handlers.go).
    """
    why = unconfigurable(test)
    if why:
        return [("", SKIP, why)]
    stamp = test.session.run
    kept = test.add_credential(f"{stamp}-kept", test.storage)
    forgotten = test.finished_job()
    writer = test.add_credential(f"{stamp}-interrupted", test.storage)
    slowed = test.fed.origin_variant == "posixv2"
    text = f"Origin:\n  TransferRateLimit: {RATE_LIMIT}\n"
    rows: list[Row] = []
    job: dict[str, Any] = {}
    job_id = ""
    try:
        # origin-0 is slowed, whether or not it is the API's host.
        with (
            control.restart_with(test.origin_svc, {CONFIG_FILE: text})
            if slowed
            else contextlib.nullcontext()
        ):
            why = settle(test) if slowed else None
            if why:
                return [("interrupted", FAIL, f"once origin-0 restarted slowed, {why}")]
            status, found = test.submit(test.copy_job("src", "interrupted", writer, writer))
            if status != 201 or not found.get("job_id"):
                return [("interrupted", FAIL, f"submitting the copy: HTTP {status}: {found}")]
            job_id = str(found["job_id"])
            control.restart(test.svc)
            _, job, _ = test.call("GET", f"/jobs/{job_id}", test.token)
    except control.ControlError as e:
        return [("interrupted", FAIL, str(e))]
    why = settle(test)
    if why:
        rows.append(("interrupted", FAIL, f"once origin-0 restarted, {why}"))
        return rows
    if job.get("status") == "completed":
        rows.append(("interrupted", INCONCLUSIVE, "the copy finished before the restart"))
    elif job.get("status") != "failed" or INTERRUPTED not in str(job.get("error")):
        rows.append(
            (
                "interrupted",
                FAIL,
                f"after the restart, the job is {job.get('status')}:"
                + f" {str(job.get('error'))[:120]!r}",
            )
        )
    else:
        rows.append(("interrupted", PASS, f"failed: {INTERRUPTED}"))
    why = test.copied("kept", kept, kept)
    rows.append(("credential", FAIL, why) if why else ("credential", PASS, "the copy landed"))
    forgotten_id = str(forgotten["job_id"])
    status, _, raw = test.call("DELETE", f"/jobs/{forgotten_id}", test.token)
    if status is None or not (200 <= status < 300 or status == 409):
        rows.append(
            ("forgotten", FAIL, f"cancelling it: HTTP {status}, not 2xx or 409: {raw[:120]!r}")
        )
        return rows
    _, after, _ = test.call("GET", f"/jobs/{forgotten_id}", test.token)
    if after.get("status") != forgotten.get("status"):
        rows.append(
            (
                "forgotten",
                FAIL,
                f"cancelling a {forgotten.get('status')} job the restart forgot (HTTP {status})"
                + f" made it {after.get('status')}",
            )
        )
    else:
        rows.append(("forgotten", PASS, f"HTTP {status}, still {forgotten.get('status')}"))
    return rows


RUN: dict[str, Callable[[Test], Rows]] = {
    "ping": ping,
    "auth-none": lambda test: auth_with(test, None),
    "auth-storage-token": lambda test: auth_with(test, test.storage),
    "auth-unknown-key": auth_unknown_key,
    "credentials": credentials_scenario,
    "copy": copy,
    "job-status": job_status,
    "bad-dest-credential": bad_dest_credential,
    "unknown-credential": unknown_credential,
    "other-user": other_user,
    "path-not-allowed": path_not_allowed,
    "cancel": cancel,
    "local-get": local_get,
    "local-put": local_put,
    "subjects": subjects,
    "cancel-finished": cancel_finished,
    "events": events,
    "ssrf-and-prefixes": ssrf_and_prefixes,
    "credential-cli": credential_cli,
}


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, then remove what they made.

    The source object is put in place first.
    """
    fed = session.fed
    for origin in stores.unique(fed.origins):
        directory = f"{origin.store_of('protected-a')}/data/xfer"
        if not origin.pstore and not os.path.isdir(directory):
            die(f"framework/var/{directory} is missing; run ./fed.sh init")
    tokens = session.tokens()
    test = Test(session)
    confined_to = "the exports" if test.confined else "nothing"
    print(
        f"Transfer API at {test.url} ({test.svc}, confined to {confined_to}),"
        + f" objects under /protected-a/{test.base}/\n"
    )
    try:
        errors = stores.make_dirs(fed.origins, "protected-a", test.base, test.storage)
        errors += stores.seed_everywhere(
            fed.origins,
            "protected-a",
            test.rel("src"),
            test.data,
            test.storage,
            fed.exports["protected-a"],
        )
        if errors:
            die(f"could not put the source object in place: {first(errors)}")
        plain = [n for n in selected if n not in CONFIGURED and n != RESTARTS]
        for name in plain:
            record(results, name, guarded(RUN[name], test))
        if "oauth-clients" in selected:
            record(results, "oauth-clients", guarded(oauth_clients_disabled, test))
        configured_names = [n for n in selected if n in CONFIGURED]
        if configured_names:
            why = unconfigurable(test)
            if why:
                for name in configured_names:
                    results.add(f"{name} configured", SKIP, why)
            else:
                run_configured(test, configured_names, results)
        if RESTARTS in selected:
            record(results, RESTARTS, guarded(restart, test))
    finally:
        finish(test)
        errors = stores.empty_tree_everywhere(fed.origins, fed.exports, "data/xfer", tokens)
        if errors:
            common.warn(f"could not remove the run's objects: {first(errors)}")


def guarded(check: Callable[[Test], Rows], test: Test) -> list[Row]:
    """Return check(test)'s rows, or one failing row if it raised."""
    try:
        found = check(test)
    except ApiError as e:
        return [("", FAIL, str(e))]
    except Exception as e:  # pylint: disable=broad-exception-caught
        # A bug, or a server answering what the suite can't read: the
        # scenario fails, and the rest still run.
        traceback.print_exc()
        return [("", FAIL, f"{type(e).__name__}: {e}")]
    if isinstance(found, list):
        return found
    return [("", found[0], found[1])]


def record(results: common.Results, name: str, rows: list[Row]) -> None:
    """Record rows as scenario name's."""
    for case, status, note in rows:
        results.add(f"{name} {case}".strip(), status, note)


def finish(test: Test) -> None:
    """Remove what the scenarios made.

    That is the credentials, the OAuth2 clients (if the API's host still
    has them on), the issuer's clients, and the run's users and groups.
    """
    for credential, owner in test.credential_ids:
        test.call("DELETE", f"/credentials/{credential}", owner)
    for client_id in test.oauth_client_ids:
        test.call("DELETE", f"{OAUTH_CLIENTS}/{client_id}", test.token)
    for site, namespace, token, made in test.issuer_clients:
        failed = issuer.delete_client(site, namespace, token, made.client_id)
        if failed:
            warn(f"could not remove the issuer's client {made.client_id}: {failed}")
    if not test.user_ids and not test.group_ids:
        return
    try:
        admin = test.admin()
        for user_id in test.user_ids:
            failed = accounts.delete_user(admin, user_id)
            if failed:
                warn(f"could not remove user {user_id}: {failed}")
        for group_id in test.group_ids:
            failed = accounts.delete_group(admin, group_id)
            if failed:
                warn(f"could not remove group {group_id}: {failed}")
    except accounts.ApiError as e:
        warn(f"could not remove the run's users and groups: {e}")
