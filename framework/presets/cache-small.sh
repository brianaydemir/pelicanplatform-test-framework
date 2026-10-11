# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Bound every V2 cache's store (Cache.HighWaterMark, Cache.LowWaterMark):
# once a cache holds 128 MiB, it evicts objects until it holds 96 MiB.
# Pelican's defaults are a share of the whole disk, which no suite could
# fill, so without this no test ever sees an eviction. The blocks suite's
# cache-evicted scenario reads past the bound and checks that every object
# still comes back intact; the other suites run as usual, and may see the
# caches evict along the way. fed.sh refuses `cache-xrootd`, whose cache
# measures its filesystem, which a bind mount cannot bound, and
# `topo-standalone`, which has no cache.

CACHE_HIGH_WATER_MIB=128
CACHE_LOW_WATER_MIB=96
