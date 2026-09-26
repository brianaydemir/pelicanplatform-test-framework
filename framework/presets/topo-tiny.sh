# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# The whole federation in one container: one pelican process running the
# director, registry, origin, and cache modules. A quick smoke test; it
# skips the inter-service TLS, advertisement, and discovery paths.
#
# The cache is always V2 here, so fed.sh refuses `cache-xrootd`, and every
# preset that adds a service. `-p topo-tiny -p origin-xrootd` gives an
# XRootD origin in front of a V2 cache, which no other shape produces.

TOPOLOGY=tiny
