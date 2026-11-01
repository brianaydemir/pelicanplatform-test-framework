# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Run the full topology without a cache: director, registry, and origins
# alone, as a small federation that serves its data straight from its
# origins does. The directors then send every read to an origin where
# the namespace takes direct clients (redirectToCache in Pelican's
# director/director.go falls back to an origin with DirectReads), and
# find nothing (404) where it does not; clients that ask for a cache are
# sent to an origin. The federation suite's `directors` and `redirects`
# scenarios check both, and the transfers suite that every download, by
# every route, came from an origin. The suites that test a cache skip.
#
# fed.sh refuses what needs a cache: `topo-multi-cache`,
# `topo-site-local-cache`, `cache-mixed`, `cache-xrootd` (the kind is
# moot: say none), `cache-tiered`, `cache-memory`, `cache-small`,
# `origin-no-direct` (under which the origins serve only caches), and
# `origin-firewalled` (whose origin only a cache could reach); and the
# `tiny` and `standalone` topologies, whose caches are not cache-0.

FEDERATED_CACHE=false
