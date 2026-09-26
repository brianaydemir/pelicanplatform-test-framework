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
can pass without it. `posc`, `metadata`, `users`, and `owners` test
features that not every shape has, and skip where it hasn't. `auth` and `transfers`
come last, since their
`expired` tokens must be 70 seconds past their expiry before they are
presented; the session mints them as it starts.
"""

from . import (auth, blocks, commands, federation, listings, metadata, owners, posc, transfers,
               users)

SUITES = {
    "federation": federation,
    "commands": commands,
    "listings": listings,
    "blocks": blocks,
    "posc": posc,
    "metadata": metadata,
    "users": users,
    "owners": owners,
    "auth": auth,
    "transfers": transfers,
}
