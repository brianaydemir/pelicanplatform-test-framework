# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Put origin-0 behind a firewall: on a Docker network of its own, which
# the caches are not on, and whose name they resolve to their own
# loopback, where nothing listens on its ports, so that their every
# connection to it is refused at once, as a firewall refuses it, and
# they must ask director-0's connection broker for a connection that the
# origin makes in reverse (`origin-broker`, which this includes). That
# is what the broker is for: an origin that takes no connection from
# outside its site. The directors, the registry, the discovery host, the
# identity provider, the origin's backend, the transfer server, and the
# dev container are on both networks, as a site's own hosts would be, so
# the origin still registers, advertises, and polls the broker, and the
# tests still ask it directly; every fetch of a cache's goes through the
# broker, and the federation suite's `broker` scenario checks that the
# origin counted the connections it made in reverse, and the object
# requests over them. Compose puts the origin on the network that
# ORIGIN_0_NETWORK names, and gives the caches the loopback entry for
# the host that CACHES_BLACKHOLE names; fed.sh sets both from this.
#
# Only an XRootD cache has a broker client: its own process asks the
# broker for a connection on xrdcl-pelican's behalf, over a Unix socket
# (cache/broker_client.go in Pelican). The V2 cache has none, and could
# not reach the origin at all, so fed.sh refuses `cache-v2` and
# `cache-mixed`: add `cache-xrootd`. It refuses `topo-no-cache` too,
# which leaves no cache to reach the origin, and whatever `origin-broker`
# refuses: a native origin, another origin, and the `tiny` and
# `standalone` topologies. A cache verifies a token with the keys it
# fetches from the token's issuer, and the origins' own issuer is on
# the origin, out of the caches' reach, so fed.sh refuses the shape
# unless the exports trust an issuer the caches can reach
# (`auth-external-issuer`, which the discovery host serves), or take no
# token (`origin-public`).
#
# In v26.0.0-rc.3 (xrdcl-pelican 1.8.2), the XRootD cache is told of the
# broker by the director (X-Pelican-Broker), reports its refused
# connection to the origin, and never asks the broker for one, so
# nothing it serves comes back, the federation suite's `ready` fails,
# and the run stops there, as every V2-cache shape's does on that
# release; and the director's own brokered connections fail at its
# broker's check of their token (see `origin-broker`). The shape is for
# a release that brokers a cache's fetch: that is what the broker is
# for, and the framework works around no fault of Pelican's.

ORIGIN_BROKER=true
ORIGIN_FIREWALLED=true
ORIGIN_NAMESPACES=protected-a
