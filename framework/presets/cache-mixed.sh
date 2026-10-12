# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Mix the kinds of cache, as a federation does while its caches move
# from XRootD to Pelican's own: cache-1 runs XRootD, beside cache-0 (and
# cache-2, if any), which stay what CACHE_VARIANT says, V2 by default.
# A client that fails over from one cache to another partway through a
# download then crosses kinds, whose ETags must agree (the client suite's
# `failover`), and the directors route to both.
#
# Needs `topo-multi-cache`, for cache-1. Refuses `cache-xrootd`, with
# which nothing would be mixed, and `cache-tiered`, `cache-memory`, and
# `cache-small`, whose settings reach every cache, and which the XRootD
# cache reads otherwise, or not at all.

CACHE_1_VARIANT=xrootd
