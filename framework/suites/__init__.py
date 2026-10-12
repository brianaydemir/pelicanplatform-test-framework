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
can pass without it. `selftest` comes next, since it checks the helpers
that other suites rely on. `client` follows `commands`: both are of the
clients. `transfer-api` restarts the first origin, with the transfer
API configured otherwise, and while a job runs, and waits until every
director names it again. `freshness` restarts the origins, to change
the Cache-Control they send, and waits until the federation serves
again before the next suite. `cache-api` follows: it evicts and purges
what the V2 caches hold, and has each corrupt an object of its own.
`throttling` follows: it restarts the origins slowed, and the V2 caches
with a small scheduler, and restores them before the next suite.
`exports` follows: it restarts the origins with exports of other
capabilities, and copies off, and restores them before the next suite.
`pstore` follows: it restarts the first origin with its store bounded,
and its catalog backed up, and restores it before the next suite.
`routing` follows: under `-p topo-multi-cache`, it restarts the
directors with other ways to sort and filter the servers, and restores
them before the next suite. `director-api` follows: it waits for the
directors' health tests to settle, and under `-p topo-multi-director`
posts a cache's own ads to one director, and waits until the cache's
next ones have restored it. `registry` follows: it registers prefixes
of its own, which it removes before the next suite, and declares the
first federated cache down at the registry, and waits until every
director names it again.
`transfer-api`, `tiering`, `posc`, `metadata`, `users`, `owners`,
`sitelocal`, `collections`, `issuer`, and `accounts` test features that
not every shape has, and skip where a shape lacks them. Under POSC, `posc`
restarts the first origin with a short staging timeout; under
metadata publishing, `metadata` restarts it with retries put off and
its publish queue capped, and again with a frequent reconcile sweep;
each restores it before the next suite. Under multiuser, `users`
restarts the origins with a username claim, and restores them before
the next suite. `accounts` ends by putting up a new AUP at the issuer
origin, which for a moment gates its every user, and restores the old.
`advertise` posts ads as the servers, which briefly change where the
directors send clients, until it has seen them restored. `webapi` tries
the servers' admin routes, as no one and as an ordinary user, which
must refuse them, among them one that restarts a server. `observability`
follows: it raises the servers' log levels for a minute at most, and
under XRootD restarts XRootD at the first origin and cache, and waits
until each serves again. `webdav` comes after them, just before the
end: one of its requests, if an origin honored it, would remove
/protected-a's storage.
`auth` and `transfers` come after them, since their `expired` tokens
must be 70 seconds past their expiry before they are presented; the
session mints them as it starts. `client` runs early, after `commands`,
since its checks are of the clients, and under `-p topo-multi-cache`
it spoils a cache's copy of an object of its own, which the cache then
drops. `restart` and `keys` come last of all: `restart` restarts,
kills, and pauses servers, and stops the origins' storage service, and
puts each back; `keys` adds and rotates the origins' and a cache's keys,
which takes them out of the directors' routing until it puts the keys
back, and leaves what the servers cached of the keys for up to 15
minutes.
"""

from . import (
    accounts,
    advertise,
    auth,
    blocks,
    cache_api,
    client,
    collections,
    commands,
    director_api,
    exports,
    federation,
    freshness,
    issuer,
    keys,
    listings,
    metadata,
    names,
    observability,
    owners,
    posc,
    pstore,
    registry,
    restart,
    routing,
    selftest,
    sitelocal,
    throttling,
    tiering,
    transfer_api,
    transfers,
    users,
    webapi,
    webdav,
)

SUITES = {
    "federation": federation,
    "selftest": selftest,
    "commands": commands,
    "client": client,
    "transfer-api": transfer_api,
    "listings": listings,
    "names": names,
    "blocks": blocks,
    "freshness": freshness,
    "cache-api": cache_api,
    "throttling": throttling,
    "exports": exports,
    "pstore": pstore,
    "routing": routing,
    "director-api": director_api,
    "registry": registry,
    "tiering": tiering,
    "posc": posc,
    "metadata": metadata,
    "users": users,
    "owners": owners,
    "sitelocal": sitelocal,
    "collections": collections,
    "issuer": issuer,
    "accounts": accounts,
    "advertise": advertise,
    "webapi": webapi,
    "observability": observability,
    "webdav": webdav,
    "auth": auth,
    "transfers": transfers,
    "restart": restart,
    "keys": keys,
}
