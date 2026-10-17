# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Bound every cache's store: once a cache holds 128 MiB, it evicts objects
# until it holds 96 MiB. A V2 cache takes these as its water marks
# (Cache.HighWaterMark, Cache.LowWaterMark). An XRootD cache's water
# marks are of the whole filesystem under its store, which a bind mount
# cannot bound, so it takes them as the bounds on its own files instead
# (Cache.FilesMaxSize, Cache.FilesBaseSize; XRootD's `pfc.diskusage ...
# files`), which it checks once a minute. Pelican's defaults are a share
# of the whole disk, which no suite could fill, so without this no test
# ever sees an eviction. The blocks suite's cache-evicted scenario reads
# past the bound and checks that every object still comes back intact;
# the other suites run as usual, and may see the caches evict along the
# way. fed.sh refuses `topo-standalone`, which has no cache.

CACHE_HIGH_WATER_MIB=128
CACHE_LOW_WATER_MIB=96
