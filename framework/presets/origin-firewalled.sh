# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Put origin-0 behind a firewall, so that the caches must ask
# director-0's connection broker for a connection that the origin makes
# in reverse (`origin-broker`, which this includes). That is what the
# broker is for: an origin that takes no connection from outside its
# site. origin-0 is on a Docker network of its own (ORIGIN_0_NETWORK),
# which the caches are not on, and the caches resolve its name to their
# own loopback (CACHES_BLACKHOLE), where nothing listens on its ports, so
# that every connection to it is refused at once, as by a firewall.
# fed.sh sets both from this.
#
# The directors, the registry, the discovery host, the identity
# provider, the origin's backend, the transfer server, and the dev
# container are on both networks, as a site's own hosts would be: the
# origin still registers, advertises, and polls the broker, and the
# tests still reach it directly. Every fetch of a cache's goes through
# the broker, and the federation suite's `broker` scenario checks that
# the origin counted the connections it made in reverse, and the object
# requests over them.
#
# Only an XRootD cache has a broker client: the cache's own process asks
# the broker for a connection on xrdcl-pelican's behalf, over a Unix
# socket (cache/broker_client.go in Pelican). So fed.sh refuses
# `cache-v2` and `cache-mixed`: add `cache-xrootd`. It refuses
# `topo-no-cache` too, which leaves no cache to reach the origin, and
# whatever `origin-broker` refuses: a native origin, another origin, and
# the `tiny` and `standalone` topologies. A cache verifies a token with
# keys from the token's issuer, and the origins' own issuer is out of the
# caches' reach, so fed.sh refuses the shape unless the exports trust an
# issuer the caches can reach (`auth-external-issuer`), or take no token
# (`origin-public`).

ORIGIN_BROKER=true
ORIGIN_FIREWALLED=true
ORIGIN_NAMESPACES=protected-a
