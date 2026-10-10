"""The suites that test.py runs, in the order it runs them.

Each is a module with:

  SCENARIOS  its scenarios' names and descriptions, in the order it
             runs them
  skip(fed)  why it can test nothing in this shape (a
             testlib.common.Federation), or None
  run(session, selected, results)
             run the selected scenarios (a list of names), with the
             run's testlib.session.Session, recording rows in results (a
             testlib.common.Results)

A suite may also name REQUIRED scenarios, which run whatever test.py is
asked for. Only `federation` does: it comes first, since nothing else
can pass without it. `transfer-api`, `tiering`, `posc`, `metadata`,
`users`, `owners`, and `sitelocal` test features that not every shape
has, and skip where a shape lacks them.
`auth` and `transfers` come last, since their `expired` tokens must be
70 seconds past their expiry before they are presented; the session
mints them as it starts.
"""

from . import (
    auth,
    blocks,
    commands,
    federation,
    listings,
    metadata,
    names,
    owners,
    posc,
    sitelocal,
    tiering,
    transfer_api,
    transfers,
    users,
)

SUITES = {
    "federation": federation,
    "commands": commands,
    "transfer-api": transfer_api,
    "listings": listings,
    "names": names,
    "blocks": blocks,
    "tiering": tiering,
    "posc": posc,
    "metadata": metadata,
    "users": users,
    "owners": owners,
    "sitelocal": sitelocal,
    "auth": auth,
    "transfers": transfers,
}
