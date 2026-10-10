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
# through the broker; a cache tries it only when it cannot connect, and a
# V2 cache or a client never does. The federation suite's `broker`
# scenario checks what the director and the origin advertise.
#
# Pelican brokers only for an XRootD origin with one export, so fed.sh
# refuses native origins, and the origins export only /protected-a.

ORIGIN_BROKER=true
ORIGIN_NAMESPACES=protected-a
