# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Give every V2 cache an in-memory cache of decrypted blocks
# (Cache.MemoryCacheSize), so that a read of an object it holds comes from
# memory rather than from its encrypted store on disk. Nothing else
# changes: the blocks suite reads through that path as it does through
# the store. fed.sh refuses `cache-xrootd`, whose cache has no such
# setting, and `topo-standalone`, which has no cache.

CACHE_MEMORY_SIZE=64MB
