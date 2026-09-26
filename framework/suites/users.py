"""Who owns what the origins write, under `-p origin-multiuser` or
`-p server-unprivileged`: a PUT straight to each posixv2 origin, and the
owner of the file it leaves in the store.

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
from typing import List, Optional, Tuple

from testlib import common, credentials, stores, web
from testlib.common import die
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session

SCENARIOS = {
    "owner":        "an upload with the tests' token is the pelican user's (uid 10941)",
    "default-user": "under multiuser, one with another subject's token is xrootd's (uid 10940)",
}

# The users in the origins' image.
PELICAN_UID = 10941
XROOTD_UID = 10940


def skip(fed: common.Federation) -> Optional[str]:
    if not fed.multiuser and not fed.drop_privileges:
        return ("the origins neither switch users nor drop privileges"
                " ('-p origin-multiuser' or '-p server-unprivileged')")
    if fed.origin_variant != "posixv2":
        return f"only a posixv2 origin writes its store itself, not {fed.origin_variant}"
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


def owned(origin: common.Origin, rel: str, token: str, uid: int) -> Tuple[str, str]:
    """PUT a new object at rel in /protected-a straight to origin, and
    check that uid owns the file it leaves."""
    url = f"{origin.url}/protected-a/{rel}"
    answer = web.request("PUT", url, token=token, upload=os.urandom(4096))
    if not answer.ok:
        return FAIL, f"PUT {url}: {answer.describe()}"
    try:
        found = os.stat(f"{origin.store}/{rel}").st_uid
    except FileNotFoundError:
        return FAIL, f"{origin.svc}: framework/var/{origin.store}/{rel} is missing"
    if found != uid:
        return FAIL, f"{origin.svc}: framework/var/{origin.store}/{rel} is uid {found}'s, not {uid}'s"
    return PASS, f"{origin.svc}: uid {uid}"


def run(session: Session, selected: List[str], results: common.Results) -> None:
    fed = session.fed
    origins = stores.unique(fed.origins)
    base = f"data/users/{session.run}"
    for origin in origins:
        if not os.path.isdir(f"{origin.store}/data/users"):
            die(f"framework/var/{origin.store}/data/users is missing; run ./fed.sh init")
        common.empty_dir(f"{origin.store}/data/users")
    token = session.token("protected-a")
    other = session.path("tokens/users-other")
    credentials.mint(fed, other, "/protected-a/", fed.origin_key, fed.issuer_of("protected-a"),
                     "/", credentials.LONG, credentials.RWM, subject="someone-else")
    with open(other) as f:
        other_token = f.read().strip()
    try:
        for origin in origins:
            errors = stores.make_dirs([origin], "protected-a", base, token)
            if errors:
                die(f"could not make the test collection: {errors[0]}")
        unshown = [why for why in (shows_owners(o.store, base) for o in origins) if why]
        for name in selected:
            if name == "default-user" and not fed.multiuser:
                results.add(name, SKIP, "only under multiuser ('-p origin-multiuser')")
                continue
            if unshown:
                results.add(name, INCONCLUSIVE, common.first(unshown))
                continue
            uid, used = (PELICAN_UID, token) if name == "owner" else (XROOTD_UID, other_token)
            found = [owned(o, f"{base}/{name}-{o.svc}", used, uid) for o in origins]
            failed = [note for status, note in found if status != PASS]
            if failed:
                results.add(name, FAIL, common.first(failed))
            else:
                results.add(name, PASS, ", ".join(note for _, note in found))
    finally:
        # The origins' files are theirs, and on a Linux host beyond
        # init-data.py's reach.
        for origin in origins:
            common.empty_dir(f"{origin.store}/data/users")
