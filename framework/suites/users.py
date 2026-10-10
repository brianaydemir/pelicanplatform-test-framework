"""Who owns what the origins write, under `-p origin-multiuser` or
`-p server-unprivileged`: a PUT straight to each POSIX origin (posixv2,
or XRootD's posix), and the owner of the file it leaves in the store.

  owner         with the tests' token: the `pelican` user (uid 10941).
                Under multiuser, fed.sh maps the tests' subject to it;
                with privileges dropped, every server runs as it.
  default-user  under multiuser, with a token for another subject: the
                `xrootd` user (uid 10940), Origin.ScitokensDefaultUser

The stores are bind mounts, and whether one shows a file's owner
depends on the host: the suite first gives a file of its own to the
`pelican` user, and if the store does not show that, the rows are
inconclusive. Objects go under /protected-a/data/users/<run>/.
"""

import os
from dataclasses import dataclass
from typing import Optional

from testlib import common, credentials, stores, web
from testlib.common import die
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session

SCENARIOS = {
    "owner": "an upload with the tests' token is the pelican user's (uid 10941)",
    "default-user": "under multiuser, one with another subject's token is xrootd's (uid 10940)",
}

# The users in the origins' image.
PELICAN_UID = 10941
XROOTD_UID = 10940


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: no user switching, or no posixv2
    writes."""
    if not fed.multiuser and not fed.drop_privileges:
        return (
            "the origins neither switch users nor drop privileges"
            " ('-p origin-multiuser' or '-p server-unprivileged')"
        )
    if fed.origin_variant not in ("posixv2", "posix"):
        return f"only a POSIX origin writes its store as a user, not {fed.origin_variant}"
    if "Writes" not in fed.exports.get("protected-a", ()):
        return "/protected-a takes no writes"
    return None


def shows_owners(store: str, base: str) -> Optional[str]:
    """Why the store at framework/var/<store> does not show who owns a
    file, or None if it does."""
    probe = f"{store}/{base}/probe"
    try:
        with open(probe, "wb"):
            pass
        os.chown(probe, PELICAN_UID, PELICAN_UID)
        if os.stat(probe).st_uid != PELICAN_UID:
            return f"framework/var/{store} does not show a file's owner"
    except OSError as e:
        return f"could not give framework/var/{probe} to uid {PELICAN_UID}: {e.strerror}"
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass
    return None


def owned(origin: common.Origin, rel: str, token: str, uid: int) -> tuple[str, str]:
    """PUT a new object at rel in /protected-a straight to origin, and
    check that uid owns the file it leaves."""
    url = f"{origin.url}/protected-a/{rel}"
    answer = web.request("PUT", url, token=token, upload=os.urandom(4096))
    if not answer.ok:
        return FAIL, f"PUT {url}: {answer.describe()}"
    path = f"{origin.store_of('protected-a')}/{rel}"
    try:
        found = os.stat(path).st_uid
    except FileNotFoundError:
        return FAIL, f"{origin.svc}: framework/var/{path} is missing"
    if found != uid:
        return FAIL, f"{origin.svc}: framework/var/{path} is uid {found}'s, not {uid}'s"
    return PASS, f"{origin.svc}: uid {uid}"


@dataclass(frozen=True)
class Test:
    """What both scenarios need: the origins, the run's collection, the
    two tokens, and why the stores do not show who owns a file."""

    fed: common.Federation
    origins: list[common.Origin]
    base: str
    token: str
    other_token: str
    unshown: list[str]


def uploads_owned(test: Test, name: str, token: str, uid: int) -> tuple[str, str]:
    """owned() at every origin, of an object named for scenario name."""
    if test.unshown:
        return INCONCLUSIVE, common.first(test.unshown)
    found = [owned(o, f"{test.base}/{name}-{o.svc}", token, uid) for o in test.origins]
    failed = [note for status, note in found if status != PASS]
    if failed:
        return FAIL, common.first(failed)
    return PASS, ", ".join(note for _, note in found)


def owner(test: Test) -> tuple[str, str]:
    """An upload with the tests' token is the pelican user's."""
    return uploads_owned(test, "owner", test.token, PELICAN_UID)


def default_user(test: Test) -> tuple[str, str]:
    """Under multiuser, one with another subject's token is xrootd's."""
    if not test.fed.multiuser:
        return SKIP, "only under multiuser ('-p origin-multiuser')"
    return uploads_owned(test, "default-user", test.other_token, XROOTD_UID)


RUN = {"owner": owner, "default-user": default_user}


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios in a collection of the run's, and empty
    it."""
    fed = session.fed
    origins = stores.unique(fed.origins)
    base = f"data/users/{session.run}"
    for origin in origins:
        directory = f"{origin.store_of('protected-a')}/data/users"
        if not os.path.isdir(directory):
            die(f"framework/var/{directory} is missing; run ./fed.sh init")
        common.empty_dir(directory)
    token = session.token("protected-a")
    other = session.path("tokens/users-other")
    other_token = credentials.mint_server(fed, other, "protected-a", subject="someone-else")
    try:
        for origin in origins:
            errors = stores.make_dirs([origin], "protected-a", base, token)
            if errors:
                die(f"could not make the test collection: {errors[0]}")
        unshown = [
            why
            for why in (shows_owners(o.store_of("protected-a"), base) for o in origins)
            if why
        ]
        test = Test(fed, origins, base, token, other_token, unshown)
        for name in selected:
            results.add(name, *RUN[name](test))
    finally:
        # The origins' files are theirs, and on a Linux host beyond
        # init-data.py's reach.
        for origin in origins:
            common.empty_dir(f"{origin.store_of('protected-a')}/data/users")
