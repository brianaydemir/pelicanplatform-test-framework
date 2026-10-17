# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# The whole federation in one container: one pelican process running the
# director, registry, origin, and cache modules. A quick smoke test: the
# modules still advertise to the director, and find the federation,
# over HTTPS, but all at one host, so the checks that tell servers apart
# by host, or restart one alone, skip.
#
# The cache is always V2 here, so fed.sh refuses `cache-xrootd`. It also
# refuses `auth-external-issuer`, since no discovery host serves the
# issuer; `director-discovery`, since its director is the discovery host
# already; and every preset that adds a service but `origin-metadata`,
# `origin-metadata-tx`, and `auth-oidc`.

TOPOLOGY=tiny
