# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Have origin-0 use director-0 as its connection broker
# (Origin.EnableBroker), as an origin behind a firewall would: it
# advertises the broker, and polls it for connections to make in
# reverse. fed.sh publishes the broker in the federation's discovery
# document, gives each director the framework CA's key, with which it
# signs each brokered connection, and keeps the caches from advertising
# a broker of their own.
#
# The origin stays reachable directly: only the director always goes
# through the broker (and fails, on v26.0.0-rc.3 and main alike: its
# broker verifies the director's token against the origin's registered
# keys, so its health tests of the origin fail; see the director-api
# suite's `health`); an XRootD cache is told of it (the director's
# X-Pelican-Broker), but in v26.0.0-rc.3 (xrdcl-pelican 1.8.2) never asks
# it, even once its connection to the origin is refused; and a V2 cache
# or a client has no broker client. The federation suite's `broker`
# scenario checks what the director and the origin advertise. To send
# the caches' fetches through the broker, add `origin-firewalled`,
# which cuts the origin off from them.
#
# Pelican brokers only for an XRootD origin with one export, so fed.sh
# refuses native origins, and the origins export only /protected-a. It
# brokers origin-0 alone, in a container of its own, so fed.sh refuses
# `topo-multi-origin`, `topo-multi-owner`, and any topology but the full
# one.

ORIGIN_BROKER=true
ORIGIN_NAMESPACES=protected-a
