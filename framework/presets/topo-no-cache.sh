# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Run the full topology without a cache: director, registry, and origins
# alone, as a small federation that serves its data straight from its
# origins does. The directors then send every read to an origin, where
# the namespace takes direct clients (the object route falls back to an
# origin with DirectReads; redirectToCache in Pelican's
# director/director.go), and find nothing (404) where it does not; and
# the clients, asking for a cache, are sent to an origin instead. The
# federation suite's `directors` and `redirects` scenarios check both,
# and the transfers suite that every download, by every route, came
# from an origin. The suites that test a cache skip.
#
# fed.sh refuses what needs a cache: `topo-multi-cache`,
# `topo-site-local-cache`, `cache-mixed`, `cache-xrootd` (the kind is
# moot: say none), `cache-tiered`, `cache-memory`, `cache-small`, and
# `origin-no-direct`, under which the origins serve only caches; and the
# `tiny` and `standalone` topologies, whose caches are not cache-0's.
FEDERATED_CACHE=false
