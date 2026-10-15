"""Which Unix user the origins act as.

Under `-p origin-multiuser` or `-p server-unprivileged`: requests straight
to each POSIX origin (posixv2, or XRootD's posix), and the owners of the
files they leave in the store.

  owner             with the tests' token: the `pelican` user (uid
                    10941). Under multiuser, fed.sh maps the tests'
                    subject to it; with privileges dropped, every server
                    runs as it.
  default-user      under multiuser, with a token for another subject:
                    the `xrootd` user (uid 10940),
                    Origin.ScitokensDefaultUser
  primary-group     with the tests' token, which names no group: the
                    pelican group (gid 10941), not root's
  public-tokenless  under multiuser, a GET and a Depth 1 PROPFIND of
                    /public with no token are served, as `nobody`
  bypass            under multiuser, the xrootd user learns nothing of
                    the pelican user's file in a 0700 directory: no HTML
                    listing names it, and no 304, ETag, or Digest shows
                    it is there; nor of POSC's staging files
  posc-multiuser    under POSC and multiuser, each user's upload is its
                    own, as is its 0700 staging directory, whichever
                    user uploads first; and no upload lands in a
                    directory its user can't write
  min-id            under multiuser, tokens that the mapfile maps to
                    `root` and `daemon`, below Origin.MultiuserMinID, are
                    refused, and write nothing
  mapfile           under multiuser, the mapfile's `path` and `group`
                    rules; a mapfile edited while the origins run; and,
                    with the origins restarted with a username claim and
                    no default user, a token whose claim names a user,
                    and one whose subject matches no rule

fed.sh's mapfile (framework/var/generated/multiuser-mapfile.json) has
the rules that min-id and mapfile use, each for a subject or group that
only this suite's tokens carry. mapfile's `edited` row adds a rule to
it, for a subject of the run's, and restores it afterward. Its
`username-claim` and `unmatched` rows restart every origin, as the last
of its configuration layers (control.restart_with()), with
Origin.ScitokensUsernameClaim set and Origin.ScitokensDefaultUser
unset; afterward it restores them. They need a control helper, and a
topology other than `tiny`, whose origin is the whole federation's
process.

The stores are bind mounts, and whether one shows a file's owner
depends on the host: the suite first gives a file of its own to the
`pelican` user, and if the store does not show that, the rows that
depend on owners are inconclusive. Objects go under
/protected-a/data/users/<run>/, and mapfile's `path` row's under
/protected-a/data/users/mapped/; public-tokenless's object under
/public/data/users/<run>/.

The tokens that the scenarios present are of the session's profile, but
for their group claim, which is `wlcg.groups` in either, since that is
the claim the origins read (Origin.ScitokensGroupsClaim's default);
`public-tokenless` presents none. The suite makes its collections, and
sees whether the federation serves again, with a WLCG token either way.
"""

import contextlib
import json
import os
import stat
import time
import traceback
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any, Optional
from urllib.parse import quote

from testlib import common, control, credentials, director, jwt, stores, web, webdav
from testlib.common import Findings, die, first, warn
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session

SCENARIOS = {
    "owner": "an upload with the tests' token is the pelican user's (uid 10941)",
    "default-user": "under multiuser, one with another subject's token is xrootd's (uid 10940)",
    "primary-group": "an upload with the tests' token is in the pelican group (gid 10941)",
    "public-tokenless": "under multiuser, /public is read and listed with no token",
    "bypass": "under multiuser, no listing, 304, ETag, or Digest shows another user's file",
    "posc-multiuser": "under POSC and multiuser, each user's upload and staging are its own",
    "min-id": "under multiuser, tokens mapped to root and daemon are refused",
    "mapfile": "under multiuser, the mapfile's rules, a username claim, and a mapfile edited",
}

Row = tuple[str, str]

NAMESPACE = "protected-a"
# The scenarios that present no token (see suites/__init__.py).
ONE_PROFILE = ("public-tokenless",)

# The rows of each scenario, in order. A scenario with cases records a
# row per case, named `<scenario> <case>`.
ROWS: dict[str, tuple[str, ...]] = {
    "owner": ("owner",),
    "default-user": ("default-user",),
    "primary-group": ("primary-group",),
    "public-tokenless": ("public-tokenless get", "public-tokenless propfind"),
    "bypass": (
        "bypass get",
        "bypass html-listing",
        "bypass if-none-match",
        "bypass digest",
        "bypass staging",
    ),
    "posc-multiuser": (
        "posc-multiuser first",
        "posc-multiuser second",
        "posc-multiuser read-only-dir",
    ),
    "min-id": ("min-id root", "min-id daemon"),
    "mapfile": (
        "mapfile path",
        "mapfile group",
        "mapfile edited",
        "mapfile username-claim",
        "mapfile unmatched",
    ),
}
# The rows that need the origins restarted with a username claim and no
# default user.
RESTARTED = ("mapfile username-claim", "mapfile unmatched")

# The users in the origins' image, each with a group of its own.
PELICAN_UID = 10941
XROOTD_UID = 10940
PELICAN_GID = 10941
# Origin.MultiuserMinID's default.
MIN_ID = 1000

# The subjects of fed.sh's mapfile rules for this suite (fed.sh), and the
# group of its `group` rule. Each maps to the pelican user, but min-id's,
# which map to system accounts; any other subject maps to the xrootd
# user, the default.
MIN_ID_SUBJECTS = {"root": "users-root", "daemon": "users-daemon"}
PATH_SUBJECT = "users-path"
GROUP_SUBJECT = "users-group"
MAPPED_GROUP = "xrootd"
# Where the `path` rules map users-path to pelican, under /protected-a.
MAPPED_DIR = "data/users/mapped"

# fed.sh's mapfile, under framework/var, which the `edited` row changes
# in place.
MAPFILE = "generated/multiuser-mapfile.json"
# How long an edited mapfile may take to apply: Pelican rereads it every
# Origin.UserMapfileRefreshInterval (1m by default), and XRootD its
# configuration every 60 seconds; and how often to try.
EDIT_WAIT = 150
EDIT_INTERVAL = 10

# The restart's claim, which names the user; the subjects of its rows'
# tokens, which match no rule; and its configuration.
USERNAME_CLAIM = "users_unix_name"
CLAIM_SUBJECT = "users-claim"
UNMATCHED_SUBJECT = "users-unmatched"
RESTART_CONFIG = (
    "---\n"
    "# The users suite's (framework/suites/users.py), while it runs.\n"
    "\n"
    "Origin:\n"
    f"  ScitokensUsernameClaim: {USERNAME_CLAIM}\n"
    '  ScitokensDefaultUser: ""\n'
)
# How long an origin restarted may take to serve its data, after its web
# port answers; and the federation to serve /protected-a as before,
# after the origins are restored.
DATA_WAIT = 60
SETTLE_WAIT = 150

# A small object at every origin, to show that the federation serves
# again after the restart.
PROBE = "probe"
PROBE_SIZE = 5000

# What a refusal of a request that a token allows, but the user's
# permissions on the file do not, may be: 403, or 404 from a permission
# error.
REFUSED = (403, 404)
# What the restart's unmatched token may get: 401 from a federated
# native origin, which drops the token; 403 from a standalone one, or
# from XRootD.
UNAUTHORIZED = (401, 403)


def skip(fed: common.Federation) -> Optional[str]:
    """Return why there is nothing to test, or None if there is something.

    There is nothing if the origins neither switch users nor drop
    privileges, are not POSIX, or take no writes in /protected-a.
    """
    if not fed.multiuser and not fed.drop_privileges:
        return (
            "the origins neither switch users nor drop privileges"
            " ('-p origin-multiuser' or '-p server-unprivileged')"
        )
    if fed.origin_variant not in ("posixv2", "posix"):
        return f"only a POSIX origin writes its store as a user, not {fed.origin_variant}"
    if "Writes" not in fed.exports.get(NAMESPACE, ()):
        return "/protected-a takes no writes"
    return None


def shows_owners(store: str, base: str) -> Optional[str]:
    """Return why store does not show a file's owner, or None if it does.

    Args:
        store: The store's path, under framework/var.
        base: A directory in the store, in which to make a probe file.
    """
    probe = f"{store}/{base}/owner-probe"
    try:
        with open(probe, "wb"):
            pass
        os.chown(probe, PELICAN_UID, PELICAN_GID)
        found = os.stat(probe)
        if (found.st_uid, found.st_gid) != (PELICAN_UID, PELICAN_GID):
            return f"framework/var/{store} does not show a file's owner"
    except OSError as e:
        return f"could not give framework/var/{probe} to uid {PELICAN_UID}: {e.strerror}"
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass
    return None


def described(path: str) -> str:
    """Return who owns path, and its mode, for a message.

    The path is under framework/var.
    """
    try:
        found = os.stat(path)
    except OSError as e:
        return f"framework/var/{path}: {e.strerror}"
    return (
        f"framework/var/{path} is uid {found.st_uid}'s, mode {stat.S_IMODE(found.st_mode):04o}"
    )


def owned(origin: common.Origin, rel: str, token: str, uid: int) -> Row:
    """PUT a new object at rel in /protected-a, and check that uid owns it.

    The PUT goes straight to origin, and uid must own the file it leaves.
    """
    url = stores.url(origin, NAMESPACE, rel)
    answer = web.request("PUT", url, token=token, upload=os.urandom(4096))
    if not answer.ok:
        return FAIL, f"PUT {url}: {answer.describe()}"
    path = f"{origin.store_of(NAMESPACE)}/{rel}"
    try:
        found = os.stat(path).st_uid
    except FileNotFoundError:
        return FAIL, f"{origin.svc}: framework/var/{path} is missing"
    if found != uid:
        return FAIL, f"{origin.svc}: framework/var/{path} is uid {found}'s, not {uid}'s"
    return PASS, f"{origin.svc}: uid {uid}"


def unmapped(origin: common.Origin, rel: str, token: str) -> Optional[str]:
    """PUT a new object with a token that a mapfile rule does not map.

    This is a control for the rule. The PUT, at rel in /protected-a, goes
    straight to origin. The default user, xrootd, may own the object, or the
    PUT may be refused: under POSC, the xrootd user may be unable to stage
    (see posc_multiuser()).

    Returns:
        Why the pelican user owns what the PUT leaves, or None if it does
        not.
    """
    url = stores.url(origin, NAMESPACE, rel)
    answer = web.request("PUT", url, token=token, upload=os.urandom(4096))
    path = f"{origin.store_of(NAMESPACE)}/{rel}"
    if answer.ok and os.path.exists(path) and os.stat(path).st_uid == PELICAN_UID:
        return f"{origin.svc}: framework/var/{path} is the pelican user's, uid {PELICAN_UID}"
    return None


def made_private(path: str, mode: int) -> Optional[str]:
    """Give path to the pelican user, with mode.

    Returns:
        Why that did not take, or None if it did.
    """
    try:
        os.chown(path, PELICAN_UID, PELICAN_GID)
        os.chmod(path, mode)
        found = os.stat(path)
    except OSError as e:
        return f"could not give framework/var/{path} to uid {PELICAN_UID}: {e.strerror}"
    if found.st_uid != PELICAN_UID or stat.S_IMODE(found.st_mode) != mode:
        return f"{described(path)}, not uid {PELICAN_UID}'s, mode {mode:04o}"
    return None


def loosen(path: str) -> None:
    """Make every directory in path writable by any uid again.

    Then the private ones the suite made can be emptied, whoever the host
    makes their owner.
    """
    try:
        os.chmod(path, 0o777)  # nosec B103
        names = os.listdir(path)
    except OSError:
        return
    for name in names:
        inner = os.path.join(path, name)
        if os.path.isdir(inner) and not os.path.islink(inner):
            loosen(inner)


@dataclass(frozen=True)
class Test:
    """What the scenarios need.

    Attributes:
        origins: The origins, each store once.
        base: The run's collection, as a path in /protected-a.
        token: The tests' token for /protected-a, of the session's
            profile.
        other_token: A token for /protected-a for another subject, of the
            session's profile.
        setup: The tests' token for /protected-a, of the WLCG profile, for
            seeing whether the federation serves.
        unshown: Why the stores do not show who owns a file; empty if they
            do.
    """

    session: Session
    fed: common.Federation
    origins: list[common.Origin]
    base: str
    token: str
    other_token: str
    setup: str
    unshown: list[str]

    def rel(self, origin: common.Origin, name: str) -> str:
        """Return the path in /protected-a of origin's object named name."""
        return f"{self.base}/{name}-{origin.svc}"

    def mint(self, subject: str, extra: Optional[Mapping[str, Any]] = None) -> str:
        """Return a token like the tests' for /protected-a, but for subject.

        It also has the claims in extra. Each call's is new, since an origin
        maps a token to a user once, and keeps it. It is signed with the
        origins' key under the tests' token's header, whose kid is the key's
        in the issuer's JWKS.
        """
        claims = credentials.server_claims(
            self.fed, NAMESPACE, subject=subject, profile=self.session.profile
        )
        claims.update(extra or {})
        return jwt.encode(jwt.decode(self.token)[0], claims, key=self.fed.origin_key)

    def multiuser(self) -> Optional[str]:
        """Return why the origins do not switch users; None if they do."""
        if not self.fed.multiuser:
            return "only under multiuser ('-p origin-multiuser')"
        return None


def uploads_owned(test: Test, name: str, token: str, uid: int) -> Row:
    """Return one row of owned() at every origin, for scenario name.

    The object at each origin is named for the scenario.
    """
    if test.unshown:
        return INCONCLUSIVE, first(test.unshown)
    found = [owned(o, test.rel(o, name), token, uid) for o in test.origins]
    failed = [note for status, note in found if status != PASS]
    if failed:
        return FAIL, first(failed)
    return PASS, ", ".join(note for _, note in found)


def owner(test: Test) -> dict[str, Row]:
    """Check that an upload with the tests' token is the pelican user's."""
    return {"owner": uploads_owned(test, "owner", test.token, PELICAN_UID)}


def default_user(test: Test) -> dict[str, Row]:
    """Check that under multiuser, another subject's upload is xrootd's."""
    why = test.multiuser()
    if why:
        return {"default-user": (SKIP, why)}
    return {"default-user": uploads_owned(test, "default-user", test.other_token, XROOTD_UID)}


def primary_group(test: Test) -> dict[str, Row]:
    """Check that the tests' upload is in the pelican user's own group.

    The tests' token names no group. A native multiuser origin takes the
    group to act as from the token's first group, and acts as gid 0 if it
    has none (resolveIdentity in origin_serve/multiuser_fs.go).
    """
    if test.unshown:
        return {"primary-group": (INCONCLUSIVE, first(test.unshown))}
    found = Findings()
    for origin in test.origins:
        rel = test.rel(origin, "primary-group")
        url = stores.url(origin, NAMESPACE, rel)
        answer = web.request("PUT", url, token=test.token, upload=os.urandom(4096))
        if not answer.ok:
            found.problems.append(f"PUT {url}: {answer.describe()}")
            continue
        path = f"{origin.store_of(NAMESPACE)}/{rel}"
        try:
            gid = os.stat(path).st_gid
        except FileNotFoundError:
            found.problems.append(f"{origin.svc}: framework/var/{path} is missing")
            continue
        if gid != PELICAN_GID:
            found.problems.append(
                f"{origin.svc}: framework/var/{path} is in gid {gid}, not {PELICAN_GID}"
            )
        else:
            found.notes.append(f"{origin.svc}: gid {gid}")
    return {"primary-group": found.row("in the pelican group")}


def public_tokenless(test: Test) -> dict[str, Row]:
    """Check that /public is read and listed with no token.

    Under multiuser, at each origin, with no token: a GET of an object
    in /public must serve its bytes, and a Depth 1 PROPFIND of its
    collection must answer 207 and name it. Pelican's documentation says
    such requests run as `nobody` (Origin.ScitokensUnauthenticatedUser);
    a native origin attaches a user only from a token
    (authorizeWithContext in origin_serve/authz.go), and its filesystem
    refuses a request without one (resolveIdentity in
    origin_serve/multiuser_fs.go). An XRootD origin is the control.
    """
    rows = ROWS["public-tokenless"]
    why = test.multiuser()
    if not why and "public" not in test.fed.exports:
        why = "this shape does not export /public"
    if why:
        return {row: (SKIP, why) for row in rows}
    data = os.urandom(4096)
    rel = f"{test.base}/public-tokenless"
    for origin in test.origins:
        error = stores.write_file(origin.store_of("public"), rel, data)
        if error:
            return {row: (FAIL, f"could not put the object in place: {error}") for row in rows}
    got, listed = Findings(), Findings()
    for origin in test.origins:
        url = f"{origin.url}/public/{rel}"
        answer = web.request("GET", url)
        if answer.status != 200:
            got.problems.append(f"{origin.svc}: GET with no token got {answer.describe()}")
        elif answer.body != data:
            got.problems.append(f"{origin.svc}: GET with no token served other bytes")
        collection = f"{origin.url}/public/{test.base}/"
        answer = webdav.propfind(collection, "1")
        if answer.status != 207:
            listed.problems.append(
                f"{origin.svc}: PROPFIND with no token got {answer.describe()}, not 207"
            )
            continue
        try:
            entries = webdav.parse(answer.body)
        except ValueError as e:
            listed.problems.append(f"{origin.svc}: the PROPFIND's answer: {e}")
            continue
        if not any(e.path.endswith("/public-tokenless") for e in entries):
            listed.problems.append(f"{origin.svc}: the PROPFIND did not list the object")
    return {
        rows[0]: got.row("served with no token"),
        rows[1]: listed.row("listed with no token"),
    }


def hidden(answer: web.Response, what: str, name: str = "", sent: bytes = b"") -> list[str]:
    """Return what answer shows of a file that the xrootd user can't read.

    That is, a status other than a refusal; an ETag, Last-Modified, or
    Digest; or in its body, the file's name or its bytes.

    Args:
        answer: The answer to the xrootd user's request.
        what: The request, as a note names it.
        name: The file's name, if the body must not hold it.
        sent: The file's bytes, if the body must not hold them.
    """
    found: list[str] = []
    if answer.status not in REFUSED:
        found.append(f"{what} got {answer.describe()}, not 403 or 404")
    for header in ("ETag", "Last-Modified", "Digest"):
        if answer.header(header):
            found.append(
                f"{what} got {answer.describe()} with {header} {answer.header(header)}"
            )
    if name and name.encode() in answer.body:
        found.append(f"{what} got {answer.describe()}, naming {name}")
    if sent and sent in answer.body:
        found.append(f"{what} got {answer.describe()}, with the file's bytes")
    return found


def bypass(test: Test) -> dict[str, Row]:
    """Check that the xrootd user learns nothing of the pelican user's file.

    Under multiuser, at each origin, the pelican user's file (0600) in
    its directory (0700), which the suite makes in the store. The
    pelican user must read it (or the rows are inconclusive), and the
    xrootd user must be refused it, and learn nothing of it:

      get            a plain GET: no ETag or Last-Modified
      html-listing   a GET of the directory for HTML: no listing of it
      if-none-match  a GET with `If-None-Match: *`: no 304, and no ETag
      digest         a HEAD with `Want-Digest: crc32c`: no Digest
      staging        under POSC, the same three, of POSC's staging
                     directory for the pelican user, and a file in it

    A native origin answers each from os.Root, as its own process, before
    it asks the filesystem, which acts as the user (handleGetWithETag,
    streamDirectoryListing, and handleHeadWithChecksum in
    origin_serve/handlers.go). An XRootD origin is the control, but for
    `staging`, which is native POSC's.
    """
    rows = ROWS["bypass"]
    why = test.multiuser()
    if not why and test.unshown:
        return {row: (INCONCLUSIVE, first(test.unshown)) for row in rows}
    if why:
        return {row: (SKIP, why) for row in rows}
    name = f"secret-{test.session.run}"
    data = os.urandom(4096)
    found = {row: Findings() for row in rows}
    for origin in test.origins:
        directory = test.rel(origin, "bypass")
        store = origin.store_of(NAMESPACE)
        try:
            os.mkdir(f"{store}/{directory}")
        except OSError as e:
            raise SetupError(f"making framework/var/{store}/{directory}: {e.strerror}") from e
        error = stores.write_file(store, f"{directory}/{name}", data)
        if error:
            raise SetupError(error)
        why = made_private(f"{store}/{directory}/{name}", 0o600) or made_private(
            f"{store}/{directory}", 0o700
        )
        if why:
            for row in rows[:4]:
                found[row].doubts.append(why)
            continue
        url = stores.url(origin, NAMESPACE, directory)
        file_url = f"{url}/{name}"
        answer = web.request("GET", file_url, token=test.token)
        if answer.status != 200 or answer.body != data:
            for row in rows[:4]:
                found[row].doubts.append(
                    f"{origin.svc}: the pelican user's own GET got {answer.describe()}"
                )
            continue
        other = test.other_token
        answer = web.request("GET", file_url, token=other)
        found["bypass get"].problems += hidden(answer, f"{origin.svc}: a GET", sent=data)
        answer = web.request("GET", f"{url}/", token=other, headers={"Accept": "text/html"})
        found["bypass html-listing"].problems += hidden(
            answer, f"{origin.svc}: a GET of the directory for HTML", name
        )
        answer = web.request("GET", file_url, token=other, headers={"If-None-Match": "*"})
        found["bypass if-none-match"].problems += hidden(
            answer, f"{origin.svc}: a GET with If-None-Match: *", sent=data
        )
        answer = web.request("HEAD", file_url, token=other, headers={"Want-Digest": "crc32c"})
        found["bypass digest"].problems += hidden(
            answer, f"{origin.svc}: a HEAD with Want-Digest: crc32c"
        )
    result = {row: found[row].row("refused, and shown nothing") for row in rows[:4]}
    result["bypass staging"] = staging_hidden(test)
    return result


def staging_hidden(test: Test) -> Row:
    """Return bypass's `staging` row.

    Under POSC, at each origin, a file the suite makes (0600) in POSC's
    staging directory for the pelican user, <storage>/.pelican-posc/pelican/
    (0700), which an upload of the pelican user's first makes, if there is
    none. The xrootd user must be refused an HTML listing of that directory,
    and the file with `If-None-Match: *`, and with `Want-Digest: crc32c`,
    and learn nothing of it, as bypass's other rows say. Pelican's POSC
    hides the staging directory from its filesystem, but the paths of bypass
    read the store themselves.

    Raises:
        SetupError: The file could not be written.
    """
    if not test.fed.posc:
        return SKIP, "only under POSC ('-p origin-posc')"
    name = f"in_progress.users-{test.session.run}"
    found = Findings()
    for origin in test.origins:
        store = origin.store_of(NAMESPACE)
        staging = f"{store}/.pelican-posc/pelican"
        if not os.path.isdir(staging):
            url = stores.url(origin, NAMESPACE, test.rel(origin, "staging-seed"))
            answer = web.request("PUT", url, token=test.token, upload=os.urandom(4096))
            if not os.path.isdir(staging):
                parent = described(f"{store}/.pelican-posc")
                found.doubts.append(
                    f"{origin.svc}: no staging directory after an upload"
                    + f" ({answer.describe()}): {parent}"
                )
                continue
        path = f"{staging}/{name}"
        try:
            with open(path, "wb") as f:
                f.write(os.urandom(4096))
        except OSError as e:
            raise SetupError(f"writing framework/var/{path}: {e.strerror}") from e
        why = made_private(path, 0o600)
        if why:
            found.doubts.append(why)
            continue
        url = stores.url(origin, NAMESPACE, ".pelican-posc/pelican")
        other = test.other_token
        answer = web.request("GET", f"{url}/", token=other, headers={"Accept": "text/html"})
        found.problems += hidden(answer, f"{origin.svc}: a GET of it for HTML", name)
        answer = web.request(
            "GET", f"{url}/{name}", token=other, headers={"If-None-Match": "*"}
        )
        found.problems += hidden(answer, f"{origin.svc}: a GET with If-None-Match: *")
        answer = web.request(
            "HEAD", f"{url}/{name}", token=other, headers={"Want-Digest": "crc32c"}
        )
        found.problems += hidden(answer, f"{origin.svc}: a HEAD with Want-Digest: crc32c")
    return found.row("refused, and shown nothing")


def posc_multiuser(test: Test) -> dict[str, Row]:
    """Check that each user's upload and staging directory are its own.

    Under POSC and multiuser, at each native origin:

      first          an upload with the tests' token succeeds, and is
                     the pelican user's, as is its staging directory,
                     <storage>/.pelican-posc/pelican/, mode 0700
      second         then one with another subject's token succeeds, and
                     is the xrootd user's, as is .pelican-posc/xrootd/,
                     mode 0700
      read-only-dir  an upload with the tests' token to a directory of the
                     pelican user's, mode 0500, is refused (403), and
                     lands nothing

    POSC sits beneath the multiuser filesystem, so the first upload makes
    the staging directory and its parent, .pelican-posc, as its user, both
    0700 (ensureUserDir in origin_serve/posc.go), which locks the second
    user out of it. The rename into place, when the upload completes,
    runs as the origin's own process (poscFile.Close), which may write
    where the user may not. read-only-dir uploads as the pelican user,
    who can stage, rather than as the xrootd user, who may not.
    """
    rows = ROWS["posc-multiuser"]
    why = test.multiuser()
    if not why and not test.fed.posc:
        why = "only under POSC ('-p origin-posc')"
    if why:
        return {row: (SKIP, why) for row in rows}
    if test.unshown:
        return {row: (INCONCLUSIVE, first(test.unshown)) for row in rows}
    return {
        rows[0]: staged(test, "posc-pelican", test.token, "pelican", PELICAN_UID),
        rows[1]: staged(test, "posc-xrootd", test.other_token, "xrootd", XROOTD_UID),
        rows[2]: read_only_dir(test),
    }


def staged(test: Test, name: str, token: str, user: str, uid: int) -> Row:
    """Return posc-multiuser's `first` or `second` row.

    At each origin, an upload with token is uid's, and so is its staging
    directory, .pelican-posc/<user>/, mode 0700.
    """
    found = Findings()
    for origin in test.origins:
        store = origin.store_of(NAMESPACE)
        status, note = owned(origin, test.rel(origin, name), token, uid)
        if status != PASS:
            found.problems.append(f"{note}; {described(f'{store}/.pelican-posc')}")
            continue
        path = f"{store}/.pelican-posc/{user}"
        try:
            info = os.stat(path)
        except OSError as e:
            found.problems.append(f"{origin.svc}: framework/var/{path}: {e.strerror}")
            continue
        if info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
            found.problems.append(
                f"{origin.svc}: {described(path)}, not uid {uid}'s, mode 0700"
            )
        else:
            found.notes.append(f"{origin.svc}: uid {uid}")
    return found.row("the uploader's, as is its staging directory")


def read_only_dir(test: Test) -> Row:
    """Return posc-multiuser's `read-only-dir` row (see posc_multiuser()).

    Raises:
        SetupError: The directory could not be made.
    """
    found = Findings()
    for origin in test.origins:
        store = origin.store_of(NAMESPACE)
        directory = test.rel(origin, "posc-read-only")
        try:
            os.mkdir(f"{store}/{directory}")
        except OSError as e:
            raise SetupError(f"making framework/var/{store}/{directory}: {e.strerror}") from e
        why = made_private(f"{store}/{directory}", 0o500)
        if why:
            found.doubts.append(why)
            continue
        rel = f"{directory}/object"
        url = stores.url(origin, NAMESPACE, rel)
        answer = web.request("PUT", url, token=test.token, upload=os.urandom(4096))
        if answer.status != 403:
            found.problems.append(f"{origin.svc}: PUT got {answer.describe()}, not 403")
        if os.path.exists(f"{store}/{rel}"):
            found.problems.append(
                f"{origin.svc}: the object landed: {described(f'{store}/{rel}')}"
            )
    return found.row("refused, with nothing landed")


def min_id(test: Test) -> dict[str, Row]:
    """Check that tokens mapped to root and daemon are refused.

    Under multiuser, at each origin, an upload with a token that the
    mapfile maps to `root`, and one to `daemon`, must be refused (403),
    and leave no file, owned by anyone below Origin.MultiuserMinID or
    not; under POSC, nor a staging directory for the user. Pelican's
    lookup refuses the user (identity.WithMinID in
    origin_serve/handlers.go).
    """
    rows = ROWS["min-id"]
    why = test.multiuser()
    if why:
        return {row: (SKIP, why) for row in rows}
    result: dict[str, Row] = {}
    for row, (user, subject) in zip(rows, MIN_ID_SUBJECTS.items()):
        token = test.mint(subject)
        found = Findings()
        for origin in test.origins:
            store = origin.store_of(NAMESPACE)
            rel = test.rel(origin, f"min-id-{user}")
            url = stores.url(origin, NAMESPACE, rel)
            answer = web.request("PUT", url, token=token, upload=os.urandom(4096))
            if answer.status != 403:
                found.problems.append(f"{origin.svc}: PUT got {answer.describe()}, not 403")
            if os.path.exists(f"{store}/{rel}"):
                found.problems.append(f"{origin.svc}: {described(f'{store}/{rel}')}")
            staging = f"{store}/.pelican-posc/{user}"
            if test.fed.posc and os.path.exists(staging):
                found.problems.append(f"{origin.svc}: {described(staging)}")
        result[row] = found.row(f"refused, with nothing written as {user}")
    return result


def mapfile(test: Test) -> dict[str, Row]:
    """Check the mapfile's rules, by the owners of uploads.

    Under multiuser, at each origin, uploads whose owners show the
    mapfile's rules at work (fed.sh's, but `edited`'s):

      path            with a token for users-path, one under
                      /protected-a/data/users/mapped/ is the pelican
                      user's, and one elsewhere not (see unmapped())
      group           with a token whose wlcg.groups is [xrootd], the
                      pelican user's
      edited          with a token for a subject of the run's, not the
                      pelican user's (see unmapped()); then, once the
                      suite adds a rule mapping the subject to the
                      pelican user, the pelican user's, within EDIT_WAIT
                      seconds
      username-claim  with the origins restarted, a token whose username
                      claim names the pelican user: the pelican user's
      unmatched       with the origins restarted, a token whose subject
                      matches no rule, with no default user: refused
                      (401 or 403), with nothing written

    A native origin maps a token to a user with no path, so `path`
    rules never match (UserMapper.MapTokenToUser in
    origin_serve/user_mapfile.go).
    """
    rows = ROWS["mapfile"]
    why = test.multiuser()
    if why:
        return {row: (SKIP, why) for row in rows}
    checks: dict[str, Callable[[], Row]] = {
        "mapfile path": partial(mapped_path, test),
        "mapfile group": partial(grouped, test),
        "mapfile edited": partial(edited, test),
    }
    result: dict[str, Row] = {
        row: (INCONCLUSIVE, first(test.unshown)) if test.unshown else attempt(check)
        for row, check in checks.items()
    }
    result.update(restarted_rows(test))
    return {row: result[row] for row in rows}


def mapped_path(test: Test) -> Row:
    """Return mapfile's `path` row (see mapfile())."""
    token = test.mint(PATH_SUBJECT)
    found = Findings()
    for origin in test.origins:
        rel = f"{MAPPED_DIR}/{test.session.run}-{origin.svc}"
        status, note = owned(origin, rel, token, PELICAN_UID)
        if status != PASS:
            found.problems.append(f"under /protected-a/{MAPPED_DIR}/, {note}")
        problem = unmapped(origin, test.rel(origin, "path-elsewhere"), token)
        if problem:
            found.problems.append(f"elsewhere, {problem}")
    return found.row("the pelican user's under the path, and not elsewhere")


def grouped(test: Test) -> Row:
    """Return mapfile's `group` row (see mapfile()).

    The group is a Unix group (gid 10940), since a native origin acts as
    the token's first group, and refuses one that it can't find
    (resolveIdentity). Its claim is the one the origins read
    (Origin.ScitokensGroupsClaim's default), whatever the profile.
    """
    token = test.mint(GROUP_SUBJECT, {"wlcg.groups": [MAPPED_GROUP]})
    return uploads_owned(test, "group", token, PELICAN_UID)


def edited(test: Test) -> Row:
    """Return mapfile's `edited` row (see mapfile()).

    The mapfile is rewritten in place, and restored afterward, even on
    failure.
    """
    subject = f"users-edited-{test.session.run}"
    try:
        with open(MAPFILE, encoding="utf-8") as f:
            original = f.read()
    except OSError as e:
        return FAIL, f"reading framework/var/{MAPFILE}: {e.strerror}"
    rules = common.as_array(json.loads(original))
    if not rules:
        return FAIL, "fed.sh's mapfile has no rules"
    token = test.mint(subject)
    failed = [
        problem
        for problem in (unmapped(o, test.rel(o, "edited-before"), token) for o in test.origins)
        if problem
    ]
    if failed:
        return FAIL, f"before the edit, {first(failed)}"
    rule = {"sub": subject, "result": "pelican", "comment": "the users suite's, while it runs"}
    start = time.time()
    try:
        with open(MAPFILE, "w", encoding="utf-8") as f:
            f.write(json.dumps(rules[:1] + [rule] + rules[1:], indent=2) + "\n")
        pending = list(test.origins)
        last: dict[str, str] = {}
        attempt_no = 0
        while True:
            attempt_no += 1
            token = test.mint(subject)
            still: list[common.Origin] = []
            for origin in pending:
                rel = test.rel(origin, f"edited-{attempt_no}")
                status, note = owned(origin, rel, token, PELICAN_UID)
                if status != PASS:
                    still.append(origin)
                    last[origin.svc] = note
            pending = still
            if not pending or time.time() - start >= EDIT_WAIT:
                break
            time.sleep(EDIT_INTERVAL)
    finally:
        with open(MAPFILE, "w", encoding="utf-8") as f:
            f.write(original)
    if pending:
        return FAIL, f"{EDIT_WAIT}s after the edit, {last[pending[0].svc]}"
    return PASS, f"the pelican user's within {time.time() - start:.0f}s of the edit"


def unrestartable(test: Test) -> Optional[str]:
    """Return why the origins cannot be restarted, or None if they can.

    The restart would be with a username claim and no default user.
    """
    if test.fed.tiny:
        return "the tiny topology's origin is the whole federation's process"
    return control.unavailable()


@contextlib.contextmanager
def restarted(test: Test) -> Generator[None, None, None]:
    """Restart every origin with RESTART_CONFIG, and afterward, as it was.

    Raises:
        control.ControlError: Restarting, or restoring, failed.
    """
    with contextlib.ExitStack() as stack:
        for origin in test.fed.origins:
            stack.enter_context(
                control.restart_with(origin.svc, {"90-users.yaml": RESTART_CONFIG})
            )
        yield


def serving_data(test: Test, token: str) -> Optional[str]:
    """Return why an origin does not yet answer the probe; None if all do.

    A HEAD of the probe may get any answer but a 5xx.
    """
    for origin in test.fed.origins:
        answer = web.request(
            "HEAD", stores.url(origin, NAMESPACE, f"{test.base}/{PROBE}"), token
        )
        if answer.status is None or answer.status >= 500:
            return f"{origin.svc}: HEAD of the probe got {answer.describe()}"
    return None


def serving(test: Test) -> Optional[str]:
    """Return why /protected-a is not served as before, or None if it is.

    Every origin must answer a HEAD of the probe, every director name every
    origin for it, and every federated cache answer a HEAD of it, with the
    setup token.
    """
    rel = f"{test.base}/{PROBE}"
    for origin in test.fed.origins:
        answer = web.request("HEAD", stores.url(origin, NAMESPACE, rel), token=test.setup)
        if answer.status != 200:
            return f"{origin.svc}: HEAD of the probe got {answer.describe()}"
    path = f"/{NAMESPACE}/{rel}"
    for url in test.fed.directors:
        where = director.ask(url, "origin", path, token=test.setup)
        for origin in test.fed.origins:
            if not where.names(director.host(origin.url)):
                return f"{director.host(url)} does not name {origin.svc}: {where.describe()}"
    for cache in test.fed.federated_caches:
        answer = web.request("HEAD", f"{cache.url}/{NAMESPACE}/{quote(rel)}", token=test.setup)
        if answer.status != 200:
            return f"{cache.svc}: HEAD of the probe got {answer.describe()}"
    return None


def wait(problem: Callable[[], Optional[str]], seconds: float) -> Optional[str]:
    """Ask problem() what is wrong, every 5 seconds, until it finds nothing.

    Stop asking once seconds have passed.

    Returns:
        What it last found, or None if it found nothing.
    """
    deadline = time.time() + seconds
    found = problem()
    while found and time.time() < deadline:
        time.sleep(5)
        found = problem()
    return f"after {seconds}s, {found}" if found else None


def restarted_rows(test: Test) -> dict[str, Row]:
    """Return mapfile's `username-claim` and `unmatched` rows, by name.

    The origins are restarted for them (see mapfile()), and afterward
    restored.
    """
    why = unrestartable(test)
    if why:
        why = (
            f"needs the origins restarted with a username claim and no default user, but {why}"
        )
        return {row: (SKIP, why) for row in RESTARTED}
    for origin in test.origins:
        error = stores.write_file(
            origin.store_of(NAMESPACE), f"{test.base}/{PROBE}", os.urandom(PROBE_SIZE)
        )
        if error:
            return {
                row: (FAIL, f"could not put the probe in place: {error}") for row in RESTARTED
            }
    claim_token = test.mint(CLAIM_SUBJECT, {USERNAME_CLAIM: "pelican"})
    result: dict[str, Row] = {}
    print("\nRestarting the origins with a username claim and no default user")
    try:
        with restarted(test):
            why = wait(partial(serving_data, test, claim_token), DATA_WAIT)
            if why:
                why = f"with the origins restarted, {why}"
                result = {row: (FAIL, why) for row in RESTARTED}
            else:
                result["mapfile username-claim"] = (
                    (INCONCLUSIVE, first(test.unshown))
                    if test.unshown
                    else attempt(partial(claimed, test, claim_token))
                )
                result["mapfile unmatched"] = attempt(partial(unmatched, test))
    except control.ControlError as e:
        # Restarting, or restoring, failed: what is left unrun fails.
        for row in RESTARTED:
            result.setdefault(row, (FAIL, f"restarting the origins: {e}"))
        warn(f"restarting the origins with a username claim: {e}")
    why = wait(partial(serving, test), SETTLE_WAIT)
    if why:
        warn(f"with the origins restored, {why}")
    return result


def claimed(test: Test, token: str) -> Row:
    """Return mapfile's `username-claim` row.

    An upload with token, whose username claim names the pelican user, is
    the pelican user's. Pelican's documentation says that the claim
    overrides Origin.ScitokensDefaultUser (docs/parameters.yaml); a native
    origin with a mapfile takes it only where a rule maps it
    (UserMapper.MapTokenToUser).
    """
    return uploads_owned(test, "username-claim", token, PELICAN_UID)


def unmatched(test: Test) -> Row:
    """Return mapfile's `unmatched` row.

    An upload with a token whose subject matches no rule, and which has no
    username claim, with no default user, is refused (401 or 403), and
    writes nothing. A native origin drops the token (authConfig.loader in
    origin_serve/authz.go); XRootD refuses it for lacking the claim.
    """
    token = test.mint(UNMATCHED_SUBJECT)
    found = Findings()
    for origin in test.origins:
        rel = test.rel(origin, "unmatched")
        url = stores.url(origin, NAMESPACE, rel)
        answer = web.request("PUT", url, token=token, upload=os.urandom(4096))
        if answer.status not in UNAUTHORIZED:
            found.problems.append(f"{origin.svc}: PUT got {answer.describe()}, not 401 or 403")
        path = f"{origin.store_of(NAMESPACE)}/{rel}"
        if os.path.exists(path):
            found.problems.append(f"{origin.svc}: {described(path)}")
    return found.row("refused, with nothing written")


class SetupError(Exception):
    """A scenario's failure to put in place what it needs."""


def attempt(check: Callable[[], Row]) -> Row:
    """Return check's row, or a FAIL if it could not set up, or raised."""
    try:
        return check()
    except SetupError as e:
        return FAIL, str(e)
    except Exception as e:  # pylint: disable=broad-exception-caught
        # A bug, or a server answering what the suite can't read: the
        # check fails, and the rest still run.
        traceback.print_exc()
        return FAIL, f"{type(e).__name__}: {e}"


def attempt_all(rows: tuple[str, ...], check: Callable[[], dict[str, Row]]) -> dict[str, Row]:
    """Return check's rows.

    If it could not set up, or raised, each of rows is a FAIL.
    """
    try:
        return check()
    except SetupError as e:
        return {row: (FAIL, str(e)) for row in rows}
    except Exception as e:  # pylint: disable=broad-exception-caught
        traceback.print_exc()
        return {row: (FAIL, f"{type(e).__name__}: {e}") for row in rows}


# Each scenario takes the Test, and returns its rows (ROWS), by name, each
# a status and a note; it raises SetupError if it cannot set up.
RUN: dict[str, Callable[[Test], dict[str, Row]]] = {
    "owner": owner,
    "default-user": default_user,
    "primary-group": primary_group,
    "public-tokenless": public_tokenless,
    "bypass": bypass,
    "posc-multiuser": posc_multiuser,
    "min-id": min_id,
    "mapfile": mapfile,
}


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios in a collection of the run's.

    Empty it before and after.
    """
    fed = session.fed
    origins = stores.unique(fed.origins)
    base = f"data/users/{session.run}"
    namespaces = [ns for ns in (NAMESPACE, "public") if ns in fed.exports]

    def clean() -> None:
        # The origins' files are theirs, and on a Linux host beyond
        # init-data.py's reach; so are the private directories the suite
        # made, until it loosens them.
        for origin in origins:
            for namespace in namespaces:
                directory = f"{origin.store_of(namespace)}/data/users"
                loosen(directory)
                common.empty_dir(directory)
            staging = f"{origin.store_of(NAMESPACE)}/.pelican-posc/pelican"
            with contextlib.suppress(OSError):
                os.remove(f"{staging}/in_progress.users-{session.run}")

    for origin in origins:
        for namespace in namespaces:
            if not os.path.isdir(f"{origin.store_of(namespace)}/data/users"):
                die(
                    f"framework/var/{origin.store_of(namespace)}/data/users is missing; run ./fed.sh init"
                )
    clean()
    token = session.token(NAMESPACE)
    setup = session.setup_token(NAMESPACE)
    other = session.path("tokens/users-other")
    other_token = credentials.mint_server(
        fed, other, NAMESPACE, subject="someone-else", profile=session.profile
    )
    try:
        for directory in (base, MAPPED_DIR):
            errors = stores.make_dirs(origins, NAMESPACE, directory, setup)
            if errors:
                die(f"could not make the test collection: {errors[0]}")
        unshown = [
            why for why in (shows_owners(o.store_of(NAMESPACE), base) for o in origins) if why
        ]
        test = Test(session, fed, origins, base, token, other_token, setup, unshown)
        for name in selected:
            rows = ROWS[name]
            if len(rows) > 1:
                print(f"  {name} ...")
            found = attempt_all(rows, partial(RUN[name], test))
            for row in rows:
                results.add(row, *found[row])
    finally:
        clean()
