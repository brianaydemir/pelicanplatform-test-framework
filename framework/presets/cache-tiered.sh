# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Have the V2 caches tier objects of 16 MiB or more to object storage
# (Cache.TieringTargets): once a cache holds all of one, it uploads it to
# the `s3` service's bucket `tier`, framework/var/data/tier, drops its
# local copy, and from then on redirects clients to it, or, for cache-1,
# proxies it. The tiering suite tests it.
#
# Tiering is on Pelican's main only: build the cache image from it (see
# the README's Server images). An older cache ignores the setting, and
# the suite then skips. Remove framework/var/data/tier along with the
# caches' stores (./reset.sh does), or a cache refuses its old target.

CACHE_TIERING=true

fed_enable_profile s3
