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
asked for. A suite imports only testlib (framework/testlib/), never
another suite: what two suites share lives there.

test.py runs each suite once for each token profile
(testlib.credentials.PROFILES), WLCG's first, each time with a session
of that profile, whose tokens the scenarios present. A scenario's setup,
and its checks of what the stores hold, use the session's setup tokens,
which are WLCG's in either pass, so that a pass tests only the tokens
that the scenario presents. A suite may name ONE_PROFILE scenarios,
which run in WLCG's pass alone: those that present no storage token of
the tests' own minting but their setup's (none at all, or only
Pelican's: its web API's, its issuer's, or a server's own), and those
whose check no other profile can make. Each pass is reported as a suite
of its own: <suite>, and <suite>-scitokens. Only the first pass of a
suite with REQUIRED scenarios can stop the run.

The order, and why (a suite's passes run back to back, so it holds for
each):

  federation     first: nothing else can pass until the federation
                 serves
  selftest       checks the helpers that the other suites rely on
  commands, client
                 the clients; under `-p topo-multi-cache`, client spoils
                 a cache's copy of an object of its own, which the cache
                 then drops
  transfer-api   restarts the first origin, with the transfer API
                 configured otherwise, and while a job runs, and waits
                 until every director names it again
  listings, names, blocks
                 restart nothing
  freshness      restarts the origins, to change the Cache-Control they
                 send, and waits until the federation serves again
  cache-api      evicts and purges what the V2 caches hold, and has each
                 corrupt an object of its own
  throttling     restarts the origins slowed, and the V2 caches with a
                 small scheduler, and restores them
  exports        restarts the origins with exports of other
                 capabilities, and copies off, and restores them
  pstore         restarts the first origin with its store bounded and
                 its catalog backed up, stops it to restore a backup
                 into its store, and restores it
  routing        under `-p topo-multi-cache`, restarts the directors
                 with other ways to sort and filter the servers, and
                 restores them
  director-api   waits for the directors' health tests to settle, and
                 under `-p topo-multi-director` posts a cache's own ads
                 to one director, and waits until the cache's next ones
                 have restored it
  registry       registers prefixes of its own, which it removes, and
                 declares the first federated cache down at the
                 registry, and waits until every director names it
                 again
  tiering, posc, metadata, users, owners, sitelocal, collections,
  issuer, accounts
                 features that not every shape has; each skips where a
                 shape lacks them. posc restarts the first origin with
                 a short staging timeout; metadata restarts it with
                 retries put off and its publish queue capped, and again
                 with a frequent reconcile sweep; users restarts the
                 origins with a username claim; each restores what it
                 restarted. accounts ends by putting up a new AUP at the
                 issuer origin, which for a moment gates its every user,
                 and restores the old
  oidc           under `-p auth-oidc`, logs the identity provider's
                 users in at every server, and restarts the first origin
                 to take their groups from the provider and name one of
                 them its admin, and restores it
  advertise      posts ads as the servers, which briefly change where
                 the directors send clients, until it has seen them
                 restored
  webapi         tries the servers' admin routes, as no one and as an
                 ordinary user, which must refuse them, among them one
                 that restarts a server
  observability  raises the servers' log levels for a minute at most,
                 and under XRootD restarts XRootD at the first origin
                 and cache, and waits until each serves again
  webdav         one of its requests, if an origin honored it, would
                 remove /protected-a's storage
  auth, transfers
                 present `expired` tokens, which must be 70 seconds past
                 their expiry; the session mints them as it starts
  restart        restarts, kills, and pauses servers, and stops the
                 origins' storage service, and puts each back
  keys           last: adds and rotates the origins' and a cache's keys,
                 which takes them out of the directors' routing until it
                 puts the keys back, and leaves what the servers cached
                 of the keys for up to 15 minutes
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
    oidc,
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
    "oidc": oidc,
    "advertise": advertise,
    "webapi": webapi,
    "observability": observability,
    "webdav": webdav,
    "auth": auth,
    "transfers": transfers,
    "restart": restart,
    "keys": keys,
}
