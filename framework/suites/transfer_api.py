"""The transfer API of origin-0 (`-p origin-transfer-api`), which runs
transfer jobs on the server: its authentication, the storage
credentials it keeps for each user, third-party copies through it, the
jobs it reports, and what it confines jobs to.

Its own tokens carry the pelican.transfer scope, from origin-0's
issuer, which the suite mints with origin-0's key; storage credentials
are the server's tokens for /protected-a. Objects go under
/protected-a/data/xfer/<run>/; the suite removes them, and the
credentials it added, afterward. A job may read or write the origin's
own filesystem only through an export, so `local-get` and `local-put`
check that a job is refused one that leaves the exports.
"""

import json
import os
import time
import traceback
import uuid
from collections.abc import Callable
from typing import Any, Optional

from testlib import blocks, common, credentials, stores, web
from testlib.common import die, first
from testlib.report import FAIL, PASS
from testlib.session import Session, last_line

SCENARIOS = {
    "ping": "the public ping answers that the service is the transfer service",
    "auth-none": "a request without a token is refused",
    "auth-storage-token": "a storage token, without pelican.transfer, is refused",
    "auth-unknown-key": "a pelican.transfer token signed by a key no server knows is refused",
    "credentials": "a storage credential is listed, never shown, and its name is its own",
    "copy": "`object copy --transfer-server ... --wait`: a third-party copy, byte for byte",
    "job-status": "a finished job reports completed, and is listed",
    "bad-dest-credential": "a copy with a read-only destination credential fails, and writes nothing",
    "unknown-credential": "a job naming a credential that does not exist is refused",
    "other-user": "another user can neither see a job nor use a credential",
    "path-not-allowed": "a job outside the origin's exports is refused",
    "cancel": "cancelling an unknown job is not found; a finished one stays finished",
    "local-get": "a job may not download to the origin's own filesystem outside an export",
    "local-put": "a job may not upload from the origin's own filesystem",
}

# The transfer API's scope, and its routes.
SCOPE = "pelican.transfer"
API = "/api/v1.0/transfer"
# How long a job may take, in seconds.
JOB_WAIT = 120
# The jobs' terminal states.
FINISHED = ("completed", "failed", "cancelled")
# A key that no server knows (see credentials.keys()).
UNKNOWN_KEY = "test-keys/unknown.pem"


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: no API, or nowhere to copy to."""
    if fed.transfer_api is None:
        return "the origins run no transfer API ('-p origin-transfer-api')"
    if "Writes" not in fed.exports.get("protected-a", ()):
        return "/protected-a takes no writes, so no copy can land"
    return None


class ApiError(Exception):
    """The API refused what a scenario's setup asked of it."""


class Test:
    """The API, its tokens and credentials, and the run's objects."""

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        api = self.fed.transfer_api
        if api is None:
            die("framework/var/generated/transfer-api says the transfer API is off")
        self.url, self.issuer, self.key_dir = api
        self.base = f"data/xfer/{session.run}"
        self.storage = session.token("protected-a")
        self.read_only = session.token("protected-a", credentials.R)
        self.token = self.mint("user", common.signing_key(self.key_dir), credentials.SUBJECT)
        # The object that the jobs copy: past pstore's chunk, so that a copy
        # takes many reads and writes.
        self.data = os.urandom(blocks.CHUNK_UNDECLARED + 4321)
        self.credential_ids: list[str] = []
        self._finished_job: Optional[dict[str, Any]] = None

    def mint(self, name: str, key: str, subject: str) -> str:
        """A token for the transfer API, signed with key, for subject."""
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

    def call(
        self,
        method: str,
        path: str,
        token: Optional[str],
        body: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[int], dict[str, Any], bytes]:
        """method on the API's path: the status, the answer as a JSON
        object ({} if it is not one), and the raw answer."""
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
        """Where object name of the run is in /protected-a."""
        return f"{self.base}/{name}"

    def url_of(self, name: str) -> str:
        """The federation URL of object name of the run."""
        return f"{self.fed.url}/protected-a/{self.rel(name)}"

    def held(self, name: str) -> list[Optional[str]]:
        """What each store holds at object name (see
        stores.held_anywhere())."""
        return stores.held_anywhere(
            self.fed.origins, "protected-a", self.rel(name), {"protected-a": self.storage}
        )

    def add_credential(self, name: str, token: str) -> str:
        """Add a storage credential for the user: its ID."""
        status, found, raw = self.call(
            "POST", "/credentials", self.token, {"name": name, "access_token": token}
        )
        if status != 201 or not found.get("id"):
            raise ApiError(f"could not add credential {name}: HTTP {status}: {raw[:200]!r}")
        self.credential_ids.append(str(found["id"]))
        return str(found["id"])

    def submit(
        self, body: dict[str, Any], token: Optional[str] = None
    ) -> tuple[Optional[int], dict[str, Any]]:
        """Submit a job, as the user unless token says otherwise: the
        status and the answer."""
        status, found, _ = self.call("POST", "/jobs", token or self.token, body)
        return status, found

    def wait(self, job_id: str) -> dict[str, Any]:
        """The job's status once it is finished, or as it stands after
        JOB_WAIT seconds."""
        deadline = time.monotonic() + JOB_WAIT
        while True:
            _, found, _ = self.call("GET", f"/jobs/{job_id}", self.token)
            if found.get("status") in FINISHED or time.monotonic() >= deadline:
                return found
            time.sleep(2)

    def copy_job(self, source: str, dest: str, source_id: str, dest_id: str) -> dict[str, Any]:
        """A job that copies object source to object dest of the run."""
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
        """A copy that has finished, through the API, made once: its
        status."""
        if self._finished_job is None:
            writer = self.add_credential(f"{self.session.run}-job", self.storage)
            status, found = self.submit(self.copy_job("src", "job-copy", writer, writer))
            if status != 201 or not found.get("job_id"):
                raise ApiError(f"could not submit a copy: HTTP {status}: {found}")
            self._finished_job = self.wait(str(found["job_id"]))
            self._finished_job.setdefault("job_id", found["job_id"])
        return self._finished_job


def refused(status: Optional[int], found: dict[str, Any]) -> Optional[str]:
    """Why an answer is not the API's refusal to authenticate, or None."""
    if status not in (401, 403) or found.get("code") != "UNAUTHORIZED":
        return f"HTTP {status}, {found.get('code')}, not 401 or 403 UNAUTHORIZED"
    return None


def ping(test: Test) -> tuple[str, str]:
    """The public ping."""
    status, found, _ = test.call("GET", "/ping", None)
    if status != 200 or found.get("service") != "transfer":
        return FAIL, f"HTTP {status}: {found}"
    return PASS, str(found.get("issuer", ""))


def auth_with(test: Test, token: Optional[str]) -> tuple[str, str]:
    """Listing jobs with token must be refused."""
    status, found, _ = test.call("GET", "/jobs", token)
    why = refused(status, found)
    if why:
        return FAIL, why
    return PASS, f"HTTP {status}"


def auth_unknown_key(test: Test) -> tuple[str, str]:
    """A pelican.transfer token from a key that no server knows."""
    return auth_with(test, test.mint("unknown", UNKNOWN_KEY, credentials.SUBJECT))


def credentials_scenario(test: Test) -> tuple[str, str]:
    """A credential is listed by its ID, no answer shows its token, and a
    second one by the same name is a conflict."""
    name = f"{test.session.run}-listed"
    status, found, raw = test.call(
        "POST", "/credentials", test.token, {"name": name, "access_token": test.storage}
    )
    if status != 201 or not found.get("id"):
        return FAIL, f"adding it: HTTP {status}: {raw[:200]!r}"
    test.credential_ids.append(str(found["id"]))
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


def copy(test: Test) -> tuple[str, str]:
    """`pelican object copy` through the transfer API, waiting for it."""
    writer = test.add_credential(f"{test.session.run}-cli", test.storage)
    token_file = test.session.path("tokens/transfer-api.user.file")
    with open(token_file, "w", encoding="utf-8") as f:
        f.write(test.token)
    code, out, err = test.session.pelican_cmd(
        "object",
        "copy",
        "--transfer-server",
        test.url,
        "--transfer-server-token",
        token_file,
        "--source-credential-id",
        writer,
        "--dest-credential-id",
        writer,
        "--wait",
        "--json",
        test.url_of("src"),
        test.url_of("cli-copy"),
    )
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err or out)}"
    if common.digest(test.data) not in test.held("cli-copy"):
        return FAIL, "no origin holds the copy"
    return PASS, ""


def job_status(test: Test) -> tuple[str, str]:
    """A finished copy's status, and the list of the user's jobs."""
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
    """A copy whose destination credential may only read is accepted, and
    fails, writing nothing."""
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
    """A job naming a credential that does not exist."""
    missing = str(uuid.uuid4())
    status, found = test.submit(test.copy_job("src", "unknown", missing, missing))
    if status != 400 or found.get("code") != "INVALID_REQUEST":
        return FAIL, f"HTTP {status}, {found.get('code')}, not 400 INVALID_REQUEST"
    return PASS, "HTTP 400"


def other_user(test: Test) -> tuple[str, str]:
    """Another user (subject) sees no job of the user's, and cannot use the
    user's credentials."""
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
    writer = test.credential_ids[0]
    status, found = test.submit(test.copy_job("src", "other", writer, writer), other)
    if status != 400:
        return FAIL, f"using the user's credential: HTTP {status}, not 400: {found}"
    return PASS, ""


def path_not_allowed(test: Test) -> tuple[str, str]:
    """A copy between paths that no export of the origin holds."""
    elsewhere = f"{test.fed.url}/elsewhere/{test.rel('x')}"
    body = {
        "transfers": [
            {"operation": "copy", "source": elsewhere, "destination": f"{elsewhere}-2"}
        ]
    }
    status, found = test.submit(body)
    if status != 403 or found.get("code") != "PATH_NOT_ALLOWED":
        return FAIL, f"HTTP {status}, {found.get('code')}, not 403 PATH_NOT_ALLOWED"
    return PASS, "HTTP 403"


def cancel(test: Test) -> tuple[str, str]:
    """Cancelling a job that does not exist, then one that has finished,
    which must stay as it finished."""
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
    test: Test, transfer: dict[str, Any], landed: Callable[[], bool]
) -> tuple[str, str]:
    """A job of one transfer that leaves the exports: it must be refused,
    or fail, and landed() must find nothing written."""
    writer = test.add_credential(f"{test.session.run}-{transfer['operation']}", test.storage)
    body = {
        "transfers": [transfer],
        "source_credential_id": writer,
        "dest_credential_id": writer,
    }
    status, found = test.submit(body)
    if status == 201 and found.get("job_id"):
        job = test.wait(str(found["job_id"]))
        if job.get("status") == "completed" or landed():
            return FAIL, f"the job ran: {job.get('status')}, and wrote outside the exports"
        return PASS, f"accepted, then {job.get('status')}"
    if landed():
        return FAIL, f"refused (HTTP {status}), but something was written"
    return PASS, f"refused (HTTP {status}, {found.get('code')})"


def local_get(test: Test) -> tuple[str, str]:
    """A `get` to the origin's /data, its store's mount, which no export
    names as is: it must not write the file there."""
    name = f"local-get-{test.session.run}"
    # Every origin-0 mounts framework/var/data/origin/0 at /data.
    landed = f"data/origin/0/{name}"
    transfer = {
        "operation": "get",
        "source": test.url_of("src"),
        "destination": f"/data/{name}",
    }
    try:
        return confined(test, transfer, lambda: os.path.exists(landed))
    finally:
        try:
            os.remove(landed)
        except OSError:
            pass


def local_put(test: Test) -> tuple[str, str]:
    """A `put` from a file of the origin's own: it must not land."""
    transfer = {
        "operation": "put",
        "source": "/etc/hostname",
        "destination": test.url_of("local-put"),
    }
    return confined(test, transfer, lambda: any(test.held("local-put")))


RUN: dict[str, Callable[[Test], tuple[str, str]]] = {
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
}


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Put the source object in place, run the selected scenarios, and
    remove what they made."""
    fed = session.fed
    for origin in stores.unique(fed.origins):
        directory = f"{origin.store_of('protected-a')}/data/xfer"
        if not origin.pstore and not os.path.isdir(directory):
            die(f"framework/var/{directory} is missing; run ./fed.sh init")
    tokens = session.tokens()
    test = Test(session)
    print(f"Transfer API at {test.url}, objects under /protected-a/{test.base}/\n")
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
        for name in selected:
            try:
                status, note = RUN[name](test)
            except Exception as e:  # pylint: disable=broad-exception-caught
                # A bug, or a server answering what the suite can't read:
                # the scenario fails, and the rest still run.
                traceback.print_exc()
                status, note = FAIL, f"{type(e).__name__}: {e}"
            results.add(name, status, note)
    finally:
        for credential in test.credential_ids:
            test.call("DELETE", f"/credentials/{credential}", test.token)
        errors = stores.empty_tree_everywhere(fed.origins, fed.exports, "data/xfer", tokens)
        if errors:
            common.warn(f"could not remove the run's objects: {first(errors)}")
